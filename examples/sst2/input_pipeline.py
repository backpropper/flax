# Copyright 2020 The Flax Authors.
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

"""SST-2 input pipeline."""

# pylint: disable=too-many-arguments,import-error,too-many-instance-attributes,too-many-locals
import collections
from typing import Dict, Sequence

from absl import logging

import numpy as np
import tensorflow.compat.v2 as tf
import tensorflow_datasets as tfds
import tensorflow_text as text
from tensorflow.io import gfile


AUTOTUNE = tf.data.experimental.AUTOTUNE
DEFAULT_BUCKET_SIZE = 8

IDX_KEY = 'idx'
INPUT_KEY = 'sentence'
LABEL_KEY = 'label'
LENGTH_KEY = 'length'

PAD_TOKEN = b'<pad>'
UNK_TOKEN = b'<unk>'
BOS_TOKEN = b'<s>'
EOS_TOKEN = b'</s>'

class SST2DataSource:
  """Provides data as pre-processed batches, a vocab, and embeddings."""

  def __init__(self,
               tokenizer: text.Tokenizer = text.WhitespaceTokenizer(),
               min_freq: int = 1,
               batch_size: Optional[int] = None,
               seed: int = None,
               vocab_path: Optional[str] = None,
               embedding_path: Optional[str] = None,
               embedding_size: Optional[int] = None,
               embedding_cache_path: Optional[str] = None,
               embedding_type: str = 'glove',
               bucket_size: int = DEFAULT_BUCKET_SIZE):
    """Initializes the data source.

    Args:
      tokenizer: Which tokenizer to apply.
      min_freq: The minimum frequency of a training set word to be included in
        the vocabulary. Default: 1 (every token is included).
      batch_size: The batch size to use for batched data iterators.
      seed: The seed controlling the data order and word embedding weight
        initialization.
      vocab_path: Optional path to the vocabulary. This can be provided if the
        vocabulary was previously computed (e.g. during a training run).
      embedding_path: Path to the original (e.g., GloVe 840B) word embeddings.
      embedding_size: Dimensionality of the word embeddings (e.g., 300).
      embedding_cache_path: Path to cached, previously computed filtered word
        embeddings. If this path does not exist, filtered embeddings will be
        saved to this path.
      embedding_type: Type of the pretrained embedding, e.g. `glove`.
      bucket_size: How many different sentence lengths to bucket together for
        bucketed batching. There will be as many buckets until the longest
        sentence in the data has a bucket.
    """
    assert seed is not None, 'Please provide a seed for shuffling.'
    self.seed = seed
    self.bucket_size = bucket_size
    self.input_fields = {INPUT_KEY}
    self.tokenizer = tokenizer

    self.train_raw = loader(train_path).cache()
    self.valid_raw = loader(valid_path).cache()
    self.test_raw = loader(test_path).cache()


    if vocab_path and gfile.Exists(vocab_path):
      self.vocab = utils.load_vocabulary(gfile.Open(vocab_path, 'rb'))
    else:
      special_tokens = [
          PAD_TOKEN, UNK_TOKEN, BOS_TOKEN, EOS_TOKEN
      ]
      self.vocab = build_vocabulary(
          self.get_tokens([self.train_raw]),
          min_freq=min_freq,
          special_tokens=special_tokens)

    self.pad_idx = self.vocab.get(PAD_TOKEN, None)
    self.unk_idx = self.vocab.get(UNK_TOKEN, None)
    self.bos_idx = self.vocab.get(BOS_TOKEN, None)
    self.eos_idx = self.vocab.get(EOS_TOKEN, None)
    self.sep_idx = self.vocab.get(SEP_TOKEN, None)
    self.tf_vocab = utils.build_tf_hashtable(self.vocab, self.unk_idx)

    if embedding_cache_path and gfile.Exists(embedding_cache_path):
      self.embeddings = utils.load_cached_embeddings(embedding_cache_path)
      assert self.embeddings.shape[0] == len(self.vocab), (
          'The number of loaded embeddings does not match the vocab size. Did '
          'you load the correct cached embeddings and the correct vocabulary?'
          ' You can try not setting `embedding_cache_path` and `vocab_path` in'
          ' order to recompute them.')
    elif embedding_path:
      assert embedding_size is not None, 'Provide glove_embedding_size.'
      self.embeddings = utils.load_glove_embeddings(self.vocab, embedding_path,
                                                    embedding_size, seed)
    else:
      self.embeddings = None
      logging.warning('glove_path not provided, embeddings are random.')

    # Turn data examples into pre-processed examples by turning each sentence
    # into a sequence of token IDs. Also pre-prepend a beginning-of-sequence
    # token <s> and append an end-of-sequence token </s>.
    self.train_dataset = self.train_raw.map(
        self.prepare_example, num_parallel_calls=AUTOTUNE).cache()
    self.valid_dataset = self.valid_raw.map(
        self.prepare_example, num_parallel_calls=AUTOTUNE).cache()
    self.test_dataset = self.test_raw.map(
        self.prepare_example, num_parallel_calls=AUTOTUNE).cache()

    self.max_sentence_length = max(
        base.get_max_sentence_length(self.train_dataset, self.example_length_fn),
        base.get_max_sentence_length(self.valid_dataset, self.example_length_fn),
        base.get_max_sentence_length(self.test_dataset, self.example_length_fn))
    logging.info('Maximum sentence length across datasets: %d',
                 self.max_sentence_length)
    logging.info('Datasets loaded.')
    logging.info('First example: %r', next(iter(tfds.as_numpy(self.train_raw))))

  @property
  def input_features(self):
    """The example features used as input to a model."""
    return {INPUT_KEY}

  @property
  def padded_shapes(self):
    """The padded shapes used by batching functions."""
    return {INPUT_KEY: [None], LABEL_KEY: [], LENGTH_KEY: []}

  def example_length_fn(self, example: ExampleType) -> tf.Tensor:
    """Returns the length of the example for the purpose of the bucketing."""
    return tf.size(example[INPUT_KEY])

  def get_tokens(self,
                 datasets: Iterable[tf.data.Dataset]) -> Iterable[List[bytes]]:
    """Returns a list of tokens for all input fields in the given datasets."""

    def _tokenize_input_features(example: types.Example) -> types.Example:
      """Tokenizes all input features in an example."""
      for feature in example:
        if feature in self.input_features:
          example[feature] = self.tokenizer.tokenize(example[feature])
      return example

    for dataset in datasets:
      # Apply the tokenizer to all input features.
      tokenized_dataset = dataset.map(
          _tokenize_input_features, num_parallel_calls=constants.AUTOTUNE)
      # Yield all tokenized input features (i.e., tokenized input sentences).
      for example in tfds.as_numpy(tokenized_dataset):
        for feature in self.input_features:
          yield example[feature]

  def add_bos_eos(self, sequence: tf.Tensor) -> tf.Tensor:
    """Prepends BOS ID and appends EOS ID to a sequence of token IDs."""
    return tf.concat([[self.bos_idx], sequence, [self.eos_idx]], 0)

  def prepare_example(self, example: ExampleType) -> ExampleType:
    """Prepares an example by converting to IDs and wrapping in <s> and </s>."""
    example_input = self.tf_vocab.lookup(
        self.tokenizer.tokenize(example[INPUT_KEY]))
    example[INPUT_KEY] = self.add_bos_eos(example_input)
    example[LENGTH_KEY] = tf.size(example[INPUT_KEY])
    return example

  def get_batches(self,
                  dataset: tf.data.Dataset,
                  batch_size: int,
                  drop_remainder: bool = False,
                  shuffle: bool = False):
    """Returns an iterator with padded batches for the provided dataset."""
    if shuffle:
      buffer_size = utils.cardinality(dataset)  # The number of examples.
      dataset = dataset.shuffle(
          buffer_size, seed=self.seed, reshuffle_each_iteration=True)
    return dataset.padded_batch(
        batch_size,
        padded_shapes=self.padded_shapes,
        drop_remainder=drop_remainder)

  def get_bucketed_batches(self,
                           dataset: tf.data.Dataset,
                           batch_size: int,
                           bucket_size: int,
                           drop_remainder: bool = False,
                           shuffle: bool = False):
    """Returns an iterator with bucketed batches for the provided dataset."""
    return get_bucketed_batches(
        dataset,
        batch_size,
        bucket_size,
        self.max_sentence_length,
        self.padded_shapes,
        self.example_length_fn,
        seed=self.seed,
        shuffle=shuffle,
        drop_remainder=drop_remainder)


def get_max_sentence_length(dataset: tf.data.Dataset,
                            example_length_fn: Any) -> int:
  """Returns the maximum length in the dataset by iterating over it once."""

  def _get_max_length(max_so_far, example):
    return tf.math.maximum(max_so_far, example_length_fn(example))

  max_length = dataset.reduce(np.int32(0), _get_max_length).numpy()
  logging.info('Maximum sentence length: %d', max_length)
  return max_length


def get_bucket_boundaries(bucket_size: int, max_size: int) -> np.ndarray:
  """Bucket boundaries with `bucket_size` items per bucket, up to `max_size`.

  Example:
  ```
  get_bucket_boundaries(8, 24)
  [9, 17, 25]
  ```
  E.g., the first boundary covers items with sizes 0-8, the next boundary covers
  items with sizes 9-16, and the last bucket covers sizes 17-24. Each bucket
  covers 8 different sizes (e.g., sentence lengths).

  Args:
   bucket_size: The number of different items per bucket.
   max_size: The maximum size to be expected for a bucket.

  Returns:
    A list of (exclusive) bucket boundaries.
  """
  return np.arange(bucket_size, max_size + bucket_size, bucket_size) + 1


def get_bucketed_batches(
    dataset: tf.data.Dataset,
    batch_size: int,
    bucket_size: int,
    max_length: int,
    padded_shapes: Any,
    example_size_fn: Any,
    seed: int = None,
    shuffle: bool = False,
    drop_remainder: bool = False,
) -> tf.data.Dataset:
  """Returns padded batches of shuffled SST examples bucketed by length.

  This shuffles the examples randomly each epoch. The random order is
  deterministic and controlled by the seed.

  Batches are padded because sentences have different lengths.
  Sentences that are shorter in a batch will get 0s added at the end, until
  all sentences in the batch have the same length.

  For performance, examples of similar lengths are bucketed together. However,
  the contents of the buckets and their order is random each epoch, and
  controlled by the seed.

  Args:
    dataset: A TF Dataset with SST examples to be shuffled and batched.
    batch_size: The size of each batch. The remainder is dropped.
    bucket_size: How many different lengths go in each bucket.
    max_length: The maximum length to provide a bucket for.
    padded_shapes: A nested structure representing the shape to which the
      respective component of each input element should be padded prior to
      batching. See `tf.data.Dataset.padded_batch` for examples.
    example_size_fn: A TF function that returns the size of an example to
      determine in which bucket it goes. E.g., the sentence length.
    seed: The seed that determines the shuffling order, with a different order
      each epoch.
    shuffle: Shuffle the dataset each epoch using seed.
    drop_remainder: Drop the last batch if it is not of size batch_size.

  Returns:
    A TF Dataset containing padded bucketed batches.
  """
  if shuffle:
    assert seed is not None, 'When shuffling you must provide a seed.'

  # For bucket_size 8 and max length 24, we get bucket boundaries [9, 17, 25].
  max_length = max_length + bucket_size % max_length  # Multiple of bucket_size.
  bucket_boundaries = get_bucket_boundaries(bucket_size, max_length)
  logging.info('Batching bucket boundaries: %r', bucket_boundaries)

  # One batch size for each bucket plus one additional one (per requirement).
  bucket_batch_sizes = [batch_size] * (len(bucket_boundaries) + 1)
  bucket_fn = tf.data.experimental.bucket_by_sequence_length(
      example_size_fn,
      bucket_boundaries,
      bucket_batch_sizes,
      padded_shapes=padded_shapes,
      pad_to_bucket_boundary=True,
      drop_remainder=drop_remainder)

  if shuffle:
    # For shuffling we need to know how many training examples we have.
    num_examples = utils.cardinality(dataset)
    num_batches = num_examples // batch_size
    return dataset.shuffle(
        num_examples, seed=seed,
        reshuffle_each_iteration=True).apply(bucket_fn).shuffle(
            num_batches, seed=seed,
            reshuffle_each_iteration=True).prefetch(constants.AUTOTUNE)
  return dataset.apply(bucket_fn).prefetch(constants.AUTOTUNE)


def build_vocabulary(sequences: Iterable[Sequence[bytes]],
                     special_tokens: Sequence[bytes] = (b'<pad>', b'<unk>',
                                                        b'<s>', b'</s>'),
                     min_freq: int = 1) -> Dict[bytes, int]:
  """Returns a vocabulary of tokens with optional minimum frequency.

  Args:
    sequences: An iterable with sequences of tokens.
    special_tokens: Special tokens that will be the start of the vocabulary.
    min_freq: The minimum frequency of each token to be included. Default: 1.

  Returns:
    An ordered dictionary that maps tokens to their IDs in the vocabulary.
  """
  # Count all the tokens.
  counter = collections.Counter()
  for tokens in sequences:
    counter.update(tokens)

  # Add special tokens to the start of vocab.
  vocab = collections.OrderedDict()
  for token in special_tokens:
    vocab[token] = len(vocab)

  # Add all other tokens to the vocab if their frequency is >= min_freq.
  for token, freq in sorted(
      # Sort by frequency (from high to low), and then by token string.
      # This makes sure high frequency tokens get a low token ID.
      counter.items(),
      key=lambda token_freq: (-token_freq[1], token_freq[0])):
    if freq >= min_freq:
      vocab[token] = len(vocab)

  logging.info('Number of unfiltered tokens: %d', len(counter))
  logging.info('Vocabulary size: %d', len(vocab))
  return vocab
