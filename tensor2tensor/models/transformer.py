# coding=utf-8
# Copyright 2018 The Tensor2Tensor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Transformer model from "Attention Is All You Need".

The Transformer model consists of an encoder and a decoder. Both are stacks
of self-attention layers followed by feed-forward layers. This model yields
good results on a number of problems, especially in NLP and machine translation.

See "Attention Is All You Need" (https://arxiv.org/abs/1706.03762) for the full
description of the model and the results obtained with its early version.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# Dependency imports

from six.moves import xrange  # pylint: disable=redefined-builtin

from tensor2tensor.data_generators import librispeech
from tensor2tensor.layers import common_attention
from tensor2tensor.layers import common_hparams
from tensor2tensor.layers import common_layers
from tensor2tensor.utils import beam_search
from tensor2tensor.utils import expert_utils
from tensor2tensor.utils import registry
from tensor2tensor.utils import t2t_model

import tensorflow as tf

from tensorflow.python.util import nest


@registry.register_model
class Transformer(t2t_model.T2TModel):
  """Attention net.  See file docstring."""

  def __init__(self, *args, **kwargs):
    super(Transformer, self).__init__(*args, **kwargs)
    self.attention_weights = dict()  # For vizualizing attention heads.

  def encode(self, inputs, target_space, hparams, features=None):
    """Encode transformer inputs.

    Args:
      inputs: Transformer inputs [batch_size, input_length, input_height,
        hidden_dim] which will be flattened along the two spatial dimensions.
      target_space: scalar, target space ID.
      hparams: hyperparmeters for model.
      features: optionally pass the entire features dictionary as well.
        This is needed now for "packed" datasets.

    Returns:
      Tuple of:
          encoder_output: Encoder representation.
              [batch_size, input_length, hidden_dim]
          encoder_decoder_attention_bias: Bias and mask weights for
              encodre-decoder attention. [batch_size, input_length]
    """
    inputs = common_layers.flatten4d3d(inputs)

    encoder_input, self_attention_bias, encoder_decoder_attention_bias = (
        transformer_prepare_encoder(
            inputs, target_space, hparams, features=features))

    encoder_input = tf.nn.dropout(encoder_input,
                                  1.0 - hparams.layer_prepostprocess_dropout)

    encoder_output = transformer_encoder(
        encoder_input, self_attention_bias,
        hparams, nonpadding=features_to_nonpadding(features, "inputs"),
        save_weights_to=self.attention_weights)

    return encoder_output, encoder_decoder_attention_bias

  def decode(self,
             decoder_input,
             encoder_output,
             encoder_decoder_attention_bias,
             decoder_self_attention_bias,
             hparams,
             cache=None,
             nonpadding=None):
    """Decode Transformer outputs from encoder representation.

    Args:
      decoder_input: inputs to bottom of the model.
          [batch_size, decoder_length, hidden_dim]
      encoder_output: Encoder representation.
          [batch_size, input_length, hidden_dim]
      encoder_decoder_attention_bias: Bias and mask weights for
          encoder-decoder attention. [batch_size, input_length]
      decoder_self_attention_bias: Bias and mask weights for decoder
          self-attention. [batch_size, decoder_length]
      hparams: hyperparmeters for model.
      cache: dict, containing tensors which are the results of previous
          attentions, used for fast decoding.
      nonpadding: optional Tensor with shape [batch_size, decoder_length]

    Returns:
      Final decoder representation. [batch_size, decoder_length, hidden_dim]
    """
    decoder_input = tf.nn.dropout(decoder_input,
                                  1.0 - hparams.layer_prepostprocess_dropout)

    decoder_output = transformer_decoder(
        decoder_input,
        encoder_output,
        decoder_self_attention_bias,
        encoder_decoder_attention_bias,
        hparams,
        cache=cache,
        nonpadding=nonpadding,
        save_weights_to=self.attention_weights)

    if (common_layers.is_on_tpu() and
        hparams.mode == tf.estimator.ModeKeys.TRAIN):
      # TPU does not react kindly to extra dimensions.
      # TODO(noam): remove this once TPU is more forgiving of extra dims.
      return decoder_output
    else:
      # Expand since t2t expects 4d tensors.
      return tf.expand_dims(decoder_output, axis=2)

  def body(self, features):
    """Transformer main model_fn.

    Args:
      features: Map of features to the model. Should contain the following:
          "inputs": Transformer inputs [batch_size, input_length, hidden_dim]
          "tragets": Target decoder outputs.
              [batch_size, decoder_length, hidden_dim]
          "target_space_id"

    Returns:
      Final decoder representation. [batch_size, decoder_length, hidden_dim]
    """
    hparams = self._hparams

    if self.has_input:
      inputs = features["inputs"]
      target_space = features["target_space_id"]
      encoder_output, encoder_decoder_attention_bias = self.encode(
          inputs, target_space, hparams, features=features)
    else:
      encoder_output, encoder_decoder_attention_bias = (None, None)

    targets = features["targets"]
    targets = common_layers.flatten4d3d(targets)

    decoder_input, decoder_self_attention_bias = transformer_prepare_decoder(
        targets, hparams, features=features)

    decoder_output = self.decode(
        decoder_input,
        encoder_output,
        encoder_decoder_attention_bias,
        decoder_self_attention_bias,
        hparams,
        nonpadding=features_to_nonpadding(features, "targets"))

    expected_attentions = features.get("expected_attentions")
    if expected_attentions is not None:
      attention_loss = common_attention.encoder_decoder_attention_loss(
          expected_attentions, self.attention_weights,
          hparams.expected_attention_loss_multiplier)
      return decoder_output, {"attention_loss": attention_loss}

    return decoder_output

  def _greedy_infer(self, features, decode_length):
    """Fast version of greedy decoding.

    Args:
      features: an map of string to `Tensor`
      decode_length: an integer.  How many additional timesteps to decode.

    Returns:
      A dict of decoding results {
          "outputs": integer `Tensor` of decoded ids of shape
              [batch_size, <= decode_length] if beam_size == 1 or
              [batch_size, top_beams, <= decode_length]
          "scores": decoding log probs from the beam search,
              None if using greedy decoding (beam_size=1)
      }

    Raises:
      NotImplementedError: If there are multiple data shards.
    """
    with tf.variable_scope(self.name):
      return self._fast_decode(features, decode_length)

  def _beam_decode(self, features, decode_length, beam_size, top_beams, alpha):
    """Beam search decoding.

    Args:
      features: an map of string to `Tensor`
      decode_length: an integer.  How many additional timesteps to decode.
      beam_size: number of beams.
      top_beams: an integer. How many of the beams to return.
      alpha: Float that controls the length penalty. larger the alpha, stronger
        the preference for slonger translations.

    Returns:
      A dict of decoding results {
          "outputs": integer `Tensor` of decoded ids of shape
              [batch_size, <= decode_length] if beam_size == 1 or
              [batch_size, top_beams, <= decode_length]
          "scores": decoding log probs from the beam search,
              None if using greedy decoding (beam_size=1)
      }
    """
    with tf.variable_scope(self.name):
      return self._fast_decode(
          features, decode_length, beam_size, top_beams, alpha)

  def _fast_decode(self,
                   features,
                   decode_length,
                   beam_size=1,
                   top_beams=1,
                   alpha=1.0):
    """Fast decoding.

    Implements both greedy and beam search decoding, uses beam search iff
    beam_size > 1, otherwise beam search related arguments are ignored.

    Args:
      features: a map of string to model  features.
      decode_length: an integer.  How many additional timesteps to decode.
      beam_size: number of beams.
      top_beams: an integer. How many of the beams to return.
      alpha: Float that controls the length penalty. larger the alpha, stronger
        the preference for slonger translations.

    Returns:
      A dict of decoding results {
          "outputs": integer `Tensor` of decoded ids of shape
              [batch_size, <= decode_length] if beam_size == 1 or
              [batch_size, top_beams, <= decode_length]
          "scores": decoding log probs from the beam search,
              None if using greedy decoding (beam_size=1)
      }

    Raises:
      NotImplementedError: If there are multiple data shards.
    """
    if self._num_datashards != 1:
      raise NotImplementedError("Fast decoding only supports a single shard.")
    dp = self._data_parallelism
    hparams = self._hparams
    target_modality = self._problem_hparams.target_modality

    if self.has_input:
      inputs = features["inputs"]
      if target_modality.is_class_modality:
        decode_length = 1
      else:
        decode_length = common_layers.shape_list(inputs)[1] + decode_length

      # TODO(llion): Clean up this reshaping logic.
      inputs = tf.expand_dims(inputs, axis=1)
      if len(inputs.shape) < 5:
        inputs = tf.expand_dims(inputs, axis=4)
      s = common_layers.shape_list(inputs)
      batch_size = s[0]
      inputs = tf.reshape(inputs, [s[0] * s[1], s[2], s[3], s[4]])
      # _shard_features called to ensure that the variable names match
      inputs = self._shard_features({"inputs": inputs})["inputs"]
      input_modality = self._problem_hparams.input_modality["inputs"]
      with tf.variable_scope(input_modality.name):
        inputs = input_modality.bottom_sharded(inputs, dp)
      with tf.variable_scope("body"):
        encoder_output, encoder_decoder_attention_bias = dp(
            self.encode, inputs, features["target_space_id"], hparams,
            features=features)
      encoder_output = encoder_output[0]
      encoder_decoder_attention_bias = encoder_decoder_attention_bias[0]
      partial_targets = None
    else:
      # The problem has no inputs.
      # In this case, features["inputs"] contains partial targets.
      # We force the outputs to begin with these sequences.
      encoder_output = None
      encoder_decoder_attention_bias = None
      partial_targets = tf.squeeze(tf.to_int64(features["inputs"]), [2, 3])
      partial_targets_length = common_layers.shape_list(partial_targets)[1]
      decode_length += partial_targets_length
      batch_size = tf.shape(partial_targets)[0]

    if hparams.pos == "timing":
      timing_signal = common_attention.get_timing_signal_1d(
          decode_length + 1, hparams.hidden_size)

    def preprocess_targets(targets, i):
      """Performs preprocessing steps on the targets to prepare for the decoder.

      This includes:
        - Embedding the ids.
        - Flattening to 3D tensor.
        - Optionally adding timing signals.

      Args:
        targets: inputs ids to the decoder. [batch_size, 1]
        i: scalar, Step number of the decoding loop.

      Returns:
        Processed targets [batch_size, 1, hidden_dim]
      """
      # _shard_features called to ensure that the variable names match
      targets = self._shard_features({"targets": targets})["targets"]
      with tf.variable_scope(target_modality.name):
        targets = target_modality.targets_bottom_sharded(targets, dp)[0]
      targets = common_layers.flatten4d3d(targets)

      # TODO(llion): Explain! Is this even needed?
      targets = tf.cond(
          tf.equal(i, 0), lambda: tf.zeros_like(targets), lambda: targets)

      if hparams.pos == "timing":
        targets += timing_signal[:, i:i + 1]
      return targets

    decoder_self_attention_bias = (
        common_attention.attention_bias_lower_triangle(decode_length))
    if hparams.proximity_bias:
      decoder_self_attention_bias += common_attention.attention_bias_proximal(
          decode_length)

    def symbols_to_logits_fn(ids, i, cache):
      """Go from ids to logits for next symbol."""
      ids = ids[:, -1:]
      targets = tf.expand_dims(tf.expand_dims(ids, axis=2), axis=3)
      targets = preprocess_targets(targets, i)

      bias = decoder_self_attention_bias[:, :, i:i + 1, :i + 1]

      with tf.variable_scope("body"):
        body_outputs = dp(
            self.decode, targets, cache.get("encoder_output"),
            cache.get("encoder_decoder_attention_bias"),
            bias, hparams, cache,
            nonpadding=features_to_nonpadding(features, "targets"))

      with tf.variable_scope(target_modality.name):
        logits = target_modality.top_sharded(body_outputs, None, dp)[0]

      ret = tf.squeeze(logits, axis=[1, 2, 3])
      if partial_targets is not None:
        # If the position is within the given partial targets, we alter the
        # logits to always return those values.
        # A faster approach would be to process the partial targets in one
        # iteration in order to fill the corresponding parts of the cache.
        # This would require broader changes, though.
        vocab_size = tf.shape(ret)[1]
        def forced_logits():
          return tf.one_hot(tf.tile(partial_targets[:, i], [beam_size]),
                            vocab_size, 0.0, -1e9)
        ret = tf.cond(
            tf.less(i, partial_targets_length), forced_logits, lambda: ret)
      return ret, cache

    ret = fast_decode(
        encoder_output=encoder_output,
        encoder_decoder_attention_bias=encoder_decoder_attention_bias,
        symbols_to_logits_fn=symbols_to_logits_fn,
        hparams=hparams,
        decode_length=decode_length,
        vocab_size=target_modality.top_dimensionality,
        beam_size=beam_size,
        top_beams=top_beams,
        alpha=alpha,
        batch_size=batch_size)
    if partial_targets is not None:
      ret["outputs"] = ret["outputs"][:, partial_targets_length:]
    return ret


def fast_decode(encoder_output,
                encoder_decoder_attention_bias,
                symbols_to_logits_fn,
                hparams,
                decode_length,
                vocab_size,
                beam_size=1,
                top_beams=1,
                alpha=1.0,
                eos_id=beam_search.EOS_ID,
                batch_size=None):
  """Given encoder output and a symbols to logits function, does fast decoding.

  Implements both greedy and beam search decoding, uses beam search iff
  beam_size > 1, otherwise beam search related arguments are ignored.

  Args:
    encoder_output: Output from encoder.
    encoder_decoder_attention_bias: a bias tensor for use in encoder-decoder
      attention
    symbols_to_logits_fn: Incremental decoding; function mapping triple
      `(ids, step, cache)` to symbol logits.
    hparams: run hyperparameters
    decode_length: an integer.  How many additional timesteps to decode.
    vocab_size: Output vocabulary size.
    beam_size: number of beams.
    top_beams: an integer. How many of the beams to return.
    alpha: Float that controls the length penalty. larger the alpha, stronger
      the preference for slonger translations.
    eos_id: End-of-sequence symbol in beam search.
    batch_size: an integer scalar - must be passed if there is no input

  Returns:
      A dict of decoding results {
          "outputs": integer `Tensor` of decoded ids of shape
              [batch_size, <= decode_length] if top_beams == 1 or
              [batch_size, top_beams, <= decode_length] otherwise
          "scores": decoding log probs from the beam search,
              None if using greedy decoding (beam_size=1)
      }

    Raises:
      NotImplementedError: If beam size > 1 with partial targets.
  """
  if encoder_output is not None:
    batch_size = common_layers.shape_list(encoder_output)[0]

  key_channels = hparams.attention_key_channels or hparams.hidden_size
  value_channels = hparams.attention_value_channels or hparams.hidden_size
  num_layers = hparams.num_decoder_layers or hparams.num_hidden_layers

  cache = {
      "layer_%d" % layer: {
          "k": tf.zeros([batch_size, 0, key_channels]),
          "v": tf.zeros([batch_size, 0, value_channels]),
      }
      for layer in range(num_layers)
  }

  if encoder_output is not None:
    cache["encoder_output"] = encoder_output
    cache["encoder_decoder_attention_bias"] = encoder_decoder_attention_bias

  if beam_size > 1:  # Beam Search
    initial_ids = tf.zeros([batch_size], dtype=tf.int32)
    decoded_ids, scores = beam_search.beam_search(
        symbols_to_logits_fn,
        initial_ids,
        beam_size,
        decode_length,
        vocab_size,
        alpha,
        states=cache,
        eos_id=eos_id,
        stop_early=(top_beams == 1))

    if top_beams == 1:
      decoded_ids = decoded_ids[:, 0, 1:]
    else:
      decoded_ids = decoded_ids[:, :top_beams, 1:]
  else:  # Greedy

    def inner_loop(i, finished, next_id, decoded_ids, cache):
      """One step of greedy decoding."""
      logits, cache = symbols_to_logits_fn(next_id, i, cache)
      temperature = (0.0 if hparams.sampling_method == "argmax" else
                     hparams.sampling_temp)
      next_id = common_layers.sample_with_temperature(logits, temperature)
      finished |= tf.equal(next_id, eos_id)
      next_id = tf.expand_dims(next_id, axis=1)
      decoded_ids = tf.concat([decoded_ids, next_id], axis=1)
      return i + 1, finished, next_id, decoded_ids, cache

    def is_not_finished(i, finished, *_):
      return (i < decode_length) & tf.logical_not(tf.reduce_all(finished))

    decoded_ids = tf.zeros([batch_size, 0], dtype=tf.int64)
    finished = tf.fill([batch_size], False)
    next_id = tf.zeros([batch_size, 1], dtype=tf.int64)
    _, _, _, decoded_ids, _ = tf.while_loop(
        is_not_finished,
        inner_loop,
        [tf.constant(0), finished, next_id, decoded_ids, cache],
        shape_invariants=[
            tf.TensorShape([]),
            tf.TensorShape([None]),
            tf.TensorShape([None, None]),
            tf.TensorShape([None, None]),
            nest.map_structure(beam_search.get_state_shape_invariants, cache),
        ])
    scores = None

  return {"outputs": decoded_ids, "scores": scores}


@registry.register_model
class TransformerEncoder(t2t_model.T2TModel):
  """Transformer, encoder only."""

  def body(self, features):
    hparams = self._hparams
    inputs = features["inputs"]
    target_space = features["target_space_id"]

    inputs = common_layers.flatten4d3d(inputs)

    (encoder_input, encoder_self_attention_bias, _) = (
        transformer_prepare_encoder(inputs, target_space, hparams))

    encoder_input = tf.nn.dropout(encoder_input,
                                  1.0 - hparams.layer_prepostprocess_dropout)
    encoder_output = transformer_encoder(
        encoder_input, encoder_self_attention_bias, hparams,
        nonpadding=features_to_nonpadding(features, "inputs"))
    encoder_output = tf.expand_dims(encoder_output, 2)

    return encoder_output


def features_to_nonpadding(features, inputs_or_targets="inputs"):
  key = inputs_or_targets + "_segmentation"
  if features and key in features:
    return tf.minimum(features[key], 1.0)
  return None


def transformer_prepare_encoder(inputs, target_space, hparams, features=None):
  """Prepare one shard of the model for the encoder.

  Args:
    inputs: a Tensor.
    target_space: a Tensor.
    hparams: run hyperparameters
    features: optionally pass the entire features dictionary as well.
      This is needed now for "packed" datasets.

  Returns:
    encoder_input: a Tensor, bottom of encoder stack
    encoder_self_attention_bias: a bias tensor for use in encoder self-attention
    encoder_decoder_attention_bias: a bias tensor for use in encoder-decoder
      attention
  """
  ishape_static = inputs.shape.as_list()
  encoder_input = inputs
  if features and "inputs_segmentation" in features:
    # Packed dataset.  Keep the examples from seeing each other.
    inputs_segmentation = features["inputs_segmentation"]
    inputs_position = features["inputs_position"]
    targets_segmentation = features["targets_segmentation"]
    encoder_self_attention_bias = common_attention.attention_bias_same_segment(
        inputs_segmentation, inputs_segmentation)
    encoder_decoder_attention_bias = (
        common_attention.attention_bias_same_segment(
            targets_segmentation, inputs_segmentation))
  else:
    # Usual case - not a packed dataset.
    encoder_padding = common_attention.embedding_to_padding(encoder_input)
    ignore_padding = common_attention.attention_bias_ignore_padding(
        encoder_padding)
    encoder_self_attention_bias = ignore_padding
    encoder_decoder_attention_bias = ignore_padding
    inputs_position = None
  if hparams.proximity_bias:
    encoder_self_attention_bias += common_attention.attention_bias_proximal(
        common_layers.shape_list(inputs)[1])
  # Append target_space_id embedding to inputs.
  emb_target_space = common_layers.embedding(
      target_space, 32, ishape_static[-1], name="target_space_embedding")
  emb_target_space = tf.reshape(emb_target_space, [1, 1, -1])
  encoder_input += emb_target_space
  if hparams.pos == "timing":
    if inputs_position is not None:
      encoder_input = common_attention.add_timing_signal_1d_given_position(
          encoder_input, inputs_position)
    else:
      encoder_input = common_attention.add_timing_signal_1d(encoder_input)
  return (encoder_input, encoder_self_attention_bias,
          encoder_decoder_attention_bias)


def transformer_prepare_decoder(targets, hparams, features=None):
  """Prepare one shard of the model for the decoder.

  Args:
    targets: a Tensor.
    hparams: run hyperparameters
    features: optionally pass the entire features dictionary as well.
      This is needed now for "packed" datasets.

  Returns:
    decoder_input: a Tensor, bottom of decoder stack
    decoder_self_attention_bias: a bias tensor for use in encoder self-attention
  """
  if hparams.prepend_mode == "prepend_inputs_full_attention":
    decoder_self_attention_bias = (
        common_attention.attention_bias_prepend_inputs_full_attention(
            common_attention.embedding_to_padding(targets)))
  else:
    decoder_self_attention_bias = (
        common_attention.attention_bias_lower_triangle(
            common_layers.shape_list(targets)[1]))

  if features and "targets_segmentation" in features:
    # "Packed" dataset - keep the examples from seeing each other.
    targets_segmentation = features["targets_segmentation"]
    targets_position = features["targets_position"]
    decoder_self_attention_bias += common_attention.attention_bias_same_segment(
        targets_segmentation, targets_segmentation)
  else:
    targets_position = None
  if hparams.proximity_bias:
    decoder_self_attention_bias += common_attention.attention_bias_proximal(
        common_layers.shape_list(targets)[1])
  decoder_input = common_layers.shift_right_3d(targets)
  if hparams.pos == "timing":
    if targets_position is not None:
      decoder_input = common_attention.add_timing_signal_1d_given_position(
          decoder_input, targets_position)
    else:
      decoder_input = common_attention.add_timing_signal_1d(decoder_input)
  return (decoder_input, decoder_self_attention_bias)


def transformer_encoder(encoder_input,
                        encoder_self_attention_bias,
                        hparams,
                        name="encoder",
                        nonpadding=None,
                        save_weights_to=None,
                        make_image_summary=True):
  """A stack of transformer layers.

  Args:
    encoder_input: a Tensor
    encoder_self_attention_bias: bias Tensor for self-attention
       (see common_attention.attention_bias())
    hparams: hyperparameters for model
    name: a string
    nonpadding: optional Tensor with shape [batch_size, encoder_length]
      indicating what positions are not padding.  This must either be
      passed in, which we do for "packed" datasets, or inferred from
      encoder_self_attention_bias.  The knowledge about padding is used
      for pad_remover(efficiency) and to mask out padding in convoltutional
      layers.
    save_weights_to: an optional dictionary to capture attention weights
      for vizualization; the weights tensor will be appended there under
      a string key created from the variable scope (including name).
    make_image_summary: Whether to make an attention image summary.

  Returns:
    y: a Tensors
  """
  x = encoder_input
  attention_dropout_broadcast_dims = (
      common_layers.comma_separated_string_to_integer_list(
          getattr(hparams, "attention_dropout_broadcast_dims", "")))
  with tf.variable_scope(name):
    if nonpadding is not None:
      padding = 1.0 - nonpadding
    else:
      padding = common_attention.attention_bias_to_padding(
          encoder_self_attention_bias)
      nonpadding = 1.0 - padding
    pad_remover = None
    if hparams.use_pad_remover and not common_layers.is_on_tpu():
      pad_remover = expert_utils.PadRemover(padding)
    for layer in xrange(hparams.num_encoder_layers or
                        hparams.num_hidden_layers):
      with tf.variable_scope("layer_%d" % layer):
        with tf.variable_scope("self_attention"):
          y = common_attention.multihead_attention(
              common_layers.layer_preprocess(x, hparams),
              None,
              encoder_self_attention_bias,
              hparams.attention_key_channels or hparams.hidden_size,
              hparams.attention_value_channels or hparams.hidden_size,
              hparams.hidden_size,
              hparams.num_heads,
              hparams.attention_dropout,
              attention_type=hparams.self_attention_type,
              save_weights_to=save_weights_to,
              max_relative_position=hparams.max_relative_position,
              make_image_summary=make_image_summary,
              dropout_broadcast_dims=attention_dropout_broadcast_dims)
          x = common_layers.layer_postprocess(x, y, hparams)
        with tf.variable_scope("ffn"):
          y = transformer_ffn_layer(
              common_layers.layer_preprocess(x, hparams), hparams, pad_remover,
              conv_padding="SAME", nonpadding_mask=nonpadding)
          x = common_layers.layer_postprocess(x, y, hparams)
    # if normalization is done in layer_preprocess, then it shuold also be done
    # on the output, since the output can grow very large, being the sum of
    # a whole stack of unnormalized layer outputs.
    return common_layers.layer_preprocess(x, hparams)


def transformer_decoder(decoder_input,
                        encoder_output,
                        decoder_self_attention_bias,
                        encoder_decoder_attention_bias,
                        hparams,
                        cache=None,
                        name="decoder",
                        nonpadding=None,
                        save_weights_to=None,
                        make_image_summary=True):
  """A stack of transformer layers.

  Args:
    decoder_input: a Tensor
    encoder_output: a Tensor
    decoder_self_attention_bias: bias Tensor for self-attention
      (see common_attention.attention_bias())
    encoder_decoder_attention_bias: bias Tensor for encoder-decoder attention
      (see common_attention.attention_bias())
    hparams: hyperparameters for model
    cache: dict, containing tensors which are the results of previous
        attentions, used for fast decoding.
    name: a string
    nonpadding: optional Tensor with shape [batch_size, encoder_length]
      indicating what positions are not padding.  This is used
      to mask out padding in convoltutional layers.  We generally only
      need this mask for "packed" datasets, because for ordinary datasets,
      no padding is ever followed by nonpadding.
    save_weights_to: an optional dictionary to capture attention weights
      for vizualization; the weights tensor will be appended there under
      a string key created from the variable scope (including name).
    make_image_summary: Whether to make an attention image summary.

  Returns:
    y: a Tensors
  """
  x = decoder_input
  attention_dropout_broadcast_dims = (
      common_layers.comma_separated_string_to_integer_list(
          getattr(hparams, "attention_dropout_broadcast_dims", "")))
  with tf.variable_scope(name):
    for layer in xrange(hparams.num_decoder_layers or
                        hparams.num_hidden_layers):
      layer_name = "layer_%d" % layer
      layer_cache = cache[layer_name] if cache is not None else None
      with tf.variable_scope(layer_name):
        with tf.variable_scope("self_attention"):
          y = common_attention.multihead_attention(
              common_layers.layer_preprocess(x, hparams),
              None,
              decoder_self_attention_bias,
              hparams.attention_key_channels or hparams.hidden_size,
              hparams.attention_value_channels or hparams.hidden_size,
              hparams.hidden_size,
              hparams.num_heads,
              hparams.attention_dropout,
              attention_type=hparams.self_attention_type,
              save_weights_to=save_weights_to,
              max_relative_position=hparams.max_relative_position,
              cache=layer_cache,
              make_image_summary=make_image_summary,
              dropout_broadcast_dims=attention_dropout_broadcast_dims)
          x = common_layers.layer_postprocess(x, y, hparams)
        if encoder_output is not None:
          with tf.variable_scope("encdec_attention"):
            # TODO(llion): Add caching.
            y = common_attention.multihead_attention(
                common_layers.layer_preprocess(x, hparams),
                encoder_output,
                encoder_decoder_attention_bias,
                hparams.attention_key_channels or hparams.hidden_size,
                hparams.attention_value_channels or hparams.hidden_size,
                hparams.hidden_size,
                hparams.num_heads,
                hparams.attention_dropout,
                save_weights_to=save_weights_to,
                make_image_summary=make_image_summary,
                dropout_broadcast_dims=attention_dropout_broadcast_dims)
            x = common_layers.layer_postprocess(x, y, hparams)
        with tf.variable_scope("ffn"):
          y = transformer_ffn_layer(
              common_layers.layer_preprocess(x, hparams), hparams,
              conv_padding="LEFT", nonpadding_mask=nonpadding)
          x = common_layers.layer_postprocess(x, y, hparams)
    # if normalization is done in layer_preprocess, then it shuold also be done
    # on the output, since the output can grow very large, being the sum of
    # a whole stack of unnormalized layer outputs.
    return common_layers.layer_preprocess(x, hparams)


def transformer_ffn_layer(x,
                          hparams,
                          pad_remover=None,
                          conv_padding="LEFT",
                          nonpadding_mask=None):
  """Feed-forward layer in the transformer.

  Args:
    x: a Tensor of shape [batch_size, length, hparams.hidden_size]
    hparams: hyperparmeters for model
    pad_remover: an expert_utils.PadRemover object tracking the padding
      positions. If provided, when using convolutional settings, the padding
      is removed before applying the convolution, and restored afterward. This
      can give a significant speedup.
    conv_padding: a string - either "LEFT" or "SAME".
    nonpadding_mask: an optional Tensor with shape [batch_size, length].
      needed for convolutoinal layers with "SAME" padding.
      Contains 1.0 in positions corresponding to nonpadding.

  Returns:
    a Tensor of shape [batch_size, length, hparams.hidden_size]
  """
  ffn_layer = hparams.ffn_layer
  relu_dropout_broadcast_dims = (
      common_layers.comma_separated_string_to_integer_list(
          getattr(hparams, "relu_dropout_broadcast_dims", "")))
  if ffn_layer == "conv_hidden_relu":
    # Backwards compatibility
    ffn_layer = "dense_relu_dense"
  if ffn_layer == "dense_relu_dense":
    # In simple convolution mode, use `pad_remover` to speed up processing.
    if pad_remover:
      original_shape = common_layers.shape_list(x)
      # Collapse `x` across examples, and remove padding positions.
      x = tf.reshape(x, tf.concat([[-1], original_shape[2:]], axis=0))
      x = tf.expand_dims(pad_remover.remove(x), axis=0)
    conv_output = common_layers.dense_relu_dense(
        x,
        hparams.filter_size,
        hparams.hidden_size,
        dropout=hparams.relu_dropout,
        dropout_broadcast_dims=relu_dropout_broadcast_dims)
    if pad_remover:
      # Restore `conv_output` to the original shape of `x`, including padding.
      conv_output = tf.reshape(
          pad_remover.restore(tf.squeeze(conv_output, axis=0)), original_shape)
    return conv_output
  elif ffn_layer == "conv_relu_conv":
    return common_layers.conv_relu_conv(
        x,
        hparams.filter_size,
        hparams.hidden_size,
        first_kernel_size=3,
        second_kernel_size=1,
        padding=conv_padding,
        nonpadding_mask=nonpadding_mask,
        dropout=hparams.relu_dropout)
  elif ffn_layer == "parameter_attention":
    return common_attention.parameter_attention(
        x, hparams.parameter_attention_key_channels or hparams.hidden_size,
        hparams.parameter_attention_value_channels or hparams.hidden_size,
        hparams.hidden_size, hparams.filter_size, hparams.num_heads,
        hparams.attention_dropout)
  elif ffn_layer == "conv_hidden_relu_with_sepconv":
    return common_layers.conv_hidden_relu(
        x,
        hparams.filter_size,
        hparams.hidden_size,
        kernel_size=(3, 1),
        second_kernel_size=(31, 1),
        padding="LEFT",
        dropout=hparams.relu_dropout)
  else:
    assert ffn_layer == "none"
    return x


@registry.register_hparams
def transformer_base_v1():
  """Set of hyperparameters."""
  hparams = common_hparams.basic_params1()
  hparams.norm_type = "layer"
  hparams.hidden_size = 512
  hparams.batch_size = 4096
  hparams.max_length = 256
  hparams.clip_grad_norm = 0.  # i.e. no gradient clipping
  hparams.optimizer_adam_epsilon = 1e-9
  hparams.learning_rate_schedule = "legacy"
  hparams.learning_rate_decay_scheme = "noam"
  hparams.learning_rate = 0.1
  hparams.learning_rate_warmup_steps = 4000
  hparams.initializer_gain = 1.0
  hparams.num_hidden_layers = 6
  hparams.initializer = "uniform_unit_scaling"
  hparams.weight_decay = 0.0
  hparams.optimizer_adam_beta1 = 0.9
  hparams.optimizer_adam_beta2 = 0.98
  hparams.num_sampled_classes = 0
  hparams.label_smoothing = 0.1
  hparams.shared_embedding_and_softmax_weights = True
  hparams.symbol_modality_num_shards = 16

  # Add new ones like this.
  hparams.add_hparam("filter_size", 2048)
  # Layer-related flags. If zero, these fall back on hparams.num_hidden_layers.
  hparams.add_hparam("num_encoder_layers", 0)
  hparams.add_hparam("num_decoder_layers", 0)
  # Attention-related flags.
  hparams.add_hparam("num_heads", 8)
  hparams.add_hparam("attention_key_channels", 0)
  hparams.add_hparam("attention_value_channels", 0)
  hparams.add_hparam("ffn_layer", "dense_relu_dense")
  hparams.add_hparam("parameter_attention_key_channels", 0)
  hparams.add_hparam("parameter_attention_value_channels", 0)
  # All hyperparameters ending in "dropout" are automatically set to 0.0
  # when not in training mode.
  hparams.add_hparam("attention_dropout", 0.0)
  hparams.add_hparam("attention_dropout_broadcast_dims", "")
  hparams.add_hparam("relu_dropout", 0.0)
  hparams.add_hparam("relu_dropout_broadcast_dims", "")
  hparams.add_hparam("pos", "timing")  # timing, none
  hparams.add_hparam("nbr_decoder_problems", 1)
  hparams.add_hparam("proximity_bias", False)
  hparams.add_hparam("use_pad_remover", True)
  hparams.add_hparam("self_attention_type", "dot_product")
  hparams.add_hparam("max_relative_position", 0)
  return hparams


@registry.register_hparams
def transformer_base_v2():
  """Set of hyperparameters."""
  hparams = transformer_base_v1()
  hparams.layer_preprocess_sequence = "n"
  hparams.layer_postprocess_sequence = "da"
  hparams.layer_prepostprocess_dropout = 0.1
  hparams.attention_dropout = 0.1
  hparams.relu_dropout = 0.1
  hparams.learning_rate_warmup_steps = 8000
  hparams.learning_rate = 0.2
  return hparams


@registry.register_hparams
def transformer_base():
  # Update parameters here, then occasionally cut a versioned set, e.g.
  # transformer_base_v2.
  hparams = transformer_base_v2()
  hparams.optimizer_adam_beta2 = 0.997
  # New way of specifying learning rate schedule.
  # Equivalent to previous version.
  hparams.learning_rate_schedule = (
      "constant*linear_warmup*rsqrt_decay*rsqrt_hidden_size")
  hparams.learning_rate_constant = 2.0
  return hparams


@registry.register_hparams
def transformer_big():
  """HParams for transfomer big model on WMT."""
  hparams = transformer_base()
  hparams.hidden_size = 1024
  hparams.filter_size = 4096
  hparams.num_heads = 16
  hparams.layer_prepostprocess_dropout = 0.3
  return hparams


@registry.register_hparams
def transformer_big_single_gpu():
  """HParams for transformer big model for single gpu."""
  hparams = transformer_big()
  hparams.layer_prepostprocess_dropout = 0.1
  hparams.learning_rate_warmup_steps = 16000
  return hparams


@registry.register_hparams
def transformer_base_single_gpu():
  """HParams for transformer base model for single gpu."""
  hparams = transformer_base()
  hparams.batch_size = 2048
  hparams.learning_rate_warmup_steps = 16000
  return hparams


@registry.register_hparams
def transformer_parsing_base():
  """Hparams for parsing on wsj only."""
  hparams = transformer_base()
  hparams.attention_dropout = 0.2
  hparams.layer_prepostprocess_dropout = 0.2
  hparams.max_length = 512
  hparams.learning_rate_warmup_steps = 16000
  hparams.hidden_size = 1024
  hparams.learning_rate = 0.05
  hparams.shared_embedding_and_softmax_weights = False
  return hparams


@registry.register_hparams
def transformer_parsing_big():
  """HParams for parsing on wsj semi-supervised."""
  hparams = transformer_big()
  hparams.max_length = 512
  hparams.shared_source_target_embedding = False
  hparams.learning_rate_warmup_steps = 4000
  hparams.layer_prepostprocess_dropout = 0.1
  hparams.batch_size = 2048
  hparams.learning_rate = 0.05
  return hparams


@registry.register_hparams
def transformer_parsing_ice():
  """Hparams for parsing and tagging Icelandic text."""
  hparams = transformer_base_single_gpu()
  hparams.batch_size = 4096
  hparams.shared_embedding_and_softmax_weights = False
  return hparams


@registry.register_hparams
def transformer_tiny():
  hparams = transformer_base()
  hparams.num_hidden_layers = 2
  hparams.hidden_size = 128
  hparams.filter_size = 512
  hparams.num_heads = 4
  return hparams


@registry.register_hparams
def transformer_test():
  hparams = transformer_base()
  hparams.num_hidden_layers = 2
  hparams.hidden_size = 16
  hparams.filter_size = 8
  hparams.num_heads = 2
  return hparams


@registry.register_hparams
def transformer_small():
  hparams = transformer_base()
  hparams.num_hidden_layers = 2
  hparams.hidden_size = 256
  hparams.filter_size = 1024
  hparams.num_heads = 4
  return hparams


@registry.register_hparams
def transformer_l2():
  hparams = transformer_base()
  hparams.num_hidden_layers = 2
  return hparams


@registry.register_hparams
def transformer_l4():
  hparams = transformer_base()
  hparams.num_hidden_layers = 4
  return hparams


@registry.register_hparams
def transformer_l8():
  hparams = transformer_base()
  hparams.num_hidden_layers = 8
  return hparams


@registry.register_hparams
def transformer_l10():
  hparams = transformer_base()
  hparams.num_hidden_layers = 10
  return hparams


@registry.register_hparams
def transformer_h1():
  hparams = transformer_base()
  hparams.num_heads = 1
  return hparams


@registry.register_hparams
def transformer_h4():
  hparams = transformer_base()
  hparams.num_heads = 4
  return hparams


@registry.register_hparams
def transformer_h16():
  hparams = transformer_base()
  hparams.num_heads = 16
  return hparams


@registry.register_hparams
def transformer_h32():
  hparams = transformer_base()
  hparams.num_heads = 32
  return hparams


@registry.register_hparams
def transformer_k128():
  hparams = transformer_base()
  hparams.attention_key_channels = 128
  return hparams


@registry.register_hparams
def transformer_k256():
  hparams = transformer_base()
  hparams.attention_key_channels = 256
  return hparams


@registry.register_hparams
def transformer_ff1024():
  hparams = transformer_base()
  hparams.filter_size = 1024
  return hparams


@registry.register_hparams
def transformer_ff4096():
  hparams = transformer_base()
  hparams.filter_size = 4096
  return hparams


@registry.register_hparams
def transformer_dr0():
  hparams = transformer_base()
  hparams.layer_prepostprocess_dropout = 0.0
  return hparams


@registry.register_hparams
def transformer_dr2():
  hparams = transformer_base()
  hparams.layer_prepostprocess_dropout = 0.2
  return hparams


@registry.register_hparams
def transformer_ls0():
  hparams = transformer_base()
  hparams.label_smoothing = 0.0
  return hparams


@registry.register_hparams
def transformer_ls2():
  hparams = transformer_base()
  hparams.label_smoothing = 0.2
  return hparams


@registry.register_hparams
def transformer_hs256():
  hparams = transformer_base()
  hparams.hidden_size = 256
  return hparams


@registry.register_hparams
def transformer_hs1024():
  hparams = transformer_base()
  hparams.hidden_size = 1024
  return hparams


@registry.register_hparams
def transformer_big_dr1():
  hparams = transformer_base()
  hparams.hidden_size = 1024
  hparams.filter_size = 4096
  hparams.num_heads = 16
  hparams.layer_prepostprocess_dropout = 0.1
  return hparams


@registry.register_hparams
def transformer_big_enfr():
  hparams = transformer_big_dr1()
  hparams.shared_embedding_and_softmax_weights = False
  hparams.filter_size = 8192
  hparams.layer_prepostprocess_dropout = 0.1
  return hparams


@registry.register_hparams
def transformer_big_dr2():
  hparams = transformer_big_dr1()
  hparams.layer_prepostprocess_dropout = 0.2
  return hparams


@registry.register_hparams
def transformer_parameter_attention_a():
  hparams = transformer_base()
  hparams.ffn_layer = "parameter_attention"
  hparams.filter_size = 1536
  return hparams


@registry.register_hparams
def transformer_parameter_attention_b():
  hparams = transformer_base()
  hparams.ffn_layer = "parameter_attention"
  hparams.filter_size = 512
  hparams.parameter_attention_key_channels = 1024
  hparams.parameter_attention_value_channels = 1024
  hparams.num_heads = 16
  return hparams


@registry.register_hparams
def transformer_prepend_v2():
  hparams = transformer_base_v2()
  hparams.prepend_mode = "prepend_inputs_masked_attention"
  hparams.max_length = 0
  return hparams


@registry.register_hparams
def transformer_prepend_v1():
  hparams = transformer_base_v1()
  hparams.prepend_mode = "prepend_inputs_masked_attention"
  hparams.max_length = 0
  return hparams


@registry.register_hparams
def transformer_prepend():
  return transformer_prepend_v2()


@registry.register_ranged_hparams
def transformer_base_range(rhp):
  """Small range of hyperparameters."""
  # After starting from base, set intervals for some parameters.
  rhp.set_float("learning_rate", 0.3, 3.0, scale=rhp.LOG_SCALE)
  rhp.set_discrete("learning_rate_warmup_steps",
                   [1000, 2000, 4000, 8000, 16000])
  rhp.set_float("initializer_gain", 0.5, 2.0)
  rhp.set_float("optimizer_adam_beta1", 0.85, 0.95)
  rhp.set_float("optimizer_adam_beta2", 0.97, 0.99)
  rhp.set_float("weight_decay", 0.0, 2.0)


@registry.register_hparams
def transformer_relative():
  """Use relative position embeddings instead of absolute position encodings."""
  hparams = transformer_base()
  hparams.pos = None
  hparams.self_attention_type = "dot_product_relative"
  hparams.max_relative_position = 20
  return hparams


@registry.register_hparams
def transformer_relative_tiny():
  hparams = transformer_relative()
  hparams.num_hidden_layers = 2
  hparams.hidden_size = 128
  hparams.filter_size = 512
  hparams.num_heads = 4
  return hparams


@registry.register_hparams
def transformer_relative_big():
  hparams = transformer_big()
  hparams.pos = None
  hparams.self_attention_type = "dot_product_relative"
  hparams.max_relative_position = 20
  return hparams


def update_hparams_for_tpu(hparams):
  """Change hparams to be compatible with TPU training."""

  # Adafactor uses less memory than Adam.
  # switch to Adafactor with its recommended learning rate scheme.
  hparams.optimizer = "Adafactor"
  hparams.learning_rate_schedule = "rsqrt_decay"
  hparams.learning_rate_warmup_steps = 10000

  # Avoid an expensive concat on TPU.
  # >1 shards helps with faster parameter distribution on multi-GPU machines
  hparams.symbol_modality_num_shards = 1

  # Adaptive batch sizes and sequence lengths are not supported on TPU.
  # Instead, every batch has the same sequence length and the same batch size.
  # Longer sequences are dropped and shorter ones are padded.
  #
  # It is therefore suggested to use a problem where examples have been combined
  # to a longer length, e.g. the "_packed" problems.
  #
  # For problems with variable sequence lengths, this parameter controls the
  # maximum sequence length.  Shorter sequences are dropped and longer ones
  # are padded.
  #
  # For problems with fixed sequence lengths - e.g. the "_packed" problems,
  # this hyperparameter is ignored.
  hparams.max_length = 64

  # TPUs have less memory than GPUs, so decrease the batch size
  hparams.batch_size = 2048

  # Using noise broadcast in the dropout layers saves memory during training.
  hparams.attention_dropout_broadcast_dims = "0,1"  # batch, heads
  hparams.relu_dropout_broadcast_dims = "1"  # length
  hparams.layer_prepostprocess_dropout_broadcast_dims = "1"  # length


@registry.register_hparams
def transformer_tpu():
  """HParams for Transformer model on TPU."""
  hparams = transformer_base()
  update_hparams_for_tpu(hparams)
  return hparams


@registry.register_hparams
def transformer_packed_tpu():
  """Deprecated alias for transformer_tpu()."""
  return transformer_tpu()


@registry.register_hparams
def transformer_big_tpu():
  hparams = transformer_big()
  update_hparams_for_tpu(hparams)
  return hparams


@registry.register_hparams
def transformer_tiny_tpu():
  hparams = transformer_tiny()
  update_hparams_for_tpu(hparams)
  return hparams


@registry.register_ranged_hparams
def transformer_tiny_tpu_range(rhp):
  """Small range of hyperparameters."""
  rhp.set_float("learning_rate", 0.3, 3.0, scale=rhp.LOG_SCALE)
  rhp.set_float("weight_decay", 0.0, 2.0)


@registry.register_ranged_hparams
def transformer_tpu_range(rhp):
  """Small range of hyperparameters."""
  # After starting from base, set intervals for some parameters.
  rhp.set_float("learning_rate", 0.3, 3.0, scale=rhp.LOG_SCALE)
  rhp.set_discrete("learning_rate_warmup_steps",
                   [1000, 2000, 4000, 8000, 16000])
  rhp.set_float("initializer_gain", 0.5, 2.0)
  rhp.set_float("optimizer_adam_beta1", 0.85, 0.95)
  rhp.set_float("optimizer_adam_beta2", 0.97, 0.99)
  rhp.set_float("weight_decay", 0.0, 2.0)


@registry.register_hparams
def transformer_small_tpu():
  """TPU-friendly version of transformer_small.

  Returns:
    an hparams object.
  """
  hparams = transformer_small()
  update_hparams_for_tpu(hparams)
  return hparams


@registry.register_hparams
def transformer_clean():
  """No dropout, label smoothing, max_length."""
  hparams = transformer_base_v2()
  hparams.label_smoothing = 0.0
  hparams.layer_prepostprocess_dropout = 0.0
  hparams.attention_dropout = 0.0
  hparams.relu_dropout = 0.0
  hparams.max_length = 0
  return hparams


@registry.register_hparams
def transformer_clean_big():
  hparams = transformer_clean()
  hparams.hidden_size = 1024
  hparams.filter_size = 4096
  return hparams


@registry.register_hparams
def transformer_clean_big_tpu():
  hparams = transformer_clean_big()
  update_hparams_for_tpu(hparams)
  return hparams


@registry.register_hparams
def transformer_tpu_with_conv():
  """Cut down on the number of heads, and use convs instead."""
  hparams = transformer_tpu()
  hparams.num_heads = 4   # heads are expensive on tpu
  hparams.ffn_layer = "conv_relu_conv"
  return hparams


@registry.register_hparams
def transformer_lm_tpu_0():
  """Hparams for training languagemodel_lm1b8k on tpu.  92M Params."""
  hparams = transformer_clean_big()
  update_hparams_for_tpu(hparams)
  hparams.num_heads = 4   # heads are expensive on tpu
  hparams.batch_size = 4096
  hparams.shared_embedding_and_softmax_weights = False
  hparams.layer_prepostprocess_dropout = 0.1
  return hparams


@registry.register_hparams
def transformer_lm_tpu_1():
  """Hparams for training languagemodel_lm1b8k on tpu.  335M Params."""
  hparams = transformer_lm_tpu_0()
  hparams.hidden_size = 2048
  hparams.filter_size = 8192
  return hparams


@registry.register_hparams
def transformer_librispeech():
  """Hparams for training ASR model on Librispeech."""
  hparams = transformer_base()

  hparams.num_heads = 4
  hparams.filter_size = 1024
  hparams.hidden_size = 256
  hparams.num_encoder_layers = 5
  hparams.num_decoder_layers = 3
  hparams.learning_rate = 0.15
  hparams.batch_size = 6000000

  librispeech.set_librispeech_length_hparams(hparams)
  return hparams


@registry.register_hparams
def transformer_librispeech_tpu():
  """Hparams for training ASR model on Librispeech on TPU."""
  hparams = transformer_librispeech()
  update_hparams_for_tpu(hparams)

  hparams.batch_size = 32
  librispeech.set_librispeech_length_hparams(hparams)
  return hparams


@registry.register_hparams
def transformer_supervised_attention():
  """Hparams for supervised attention problems."""
  hparams = transformer_base()
  # Multiplier to the encoder-decoder expected attention loss.
  hparams.add_hparam("expected_attention_loss_multiplier", 1.0)
  return hparams
