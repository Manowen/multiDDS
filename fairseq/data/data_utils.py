# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

try:
    from collections.abc import Iterable
except ImportError:
    from collections import Iterable
import contextlib
import itertools
import os
import sys
import types

import numpy as np
import torch

from fairseq.data import iterators

def switchout(tokens, lengths, tau, dic):
    # first sample the number of words to corrupt
    max_len = tokens.size(1)

    pad_mask = (tokens == dic.pad())
    eos_mask = (tokens == dic.eos())
    bos_mask = (tokens == dic.bos())
    sample_mask = ~((~pad_mask) & (~eos_mask) & (~bos_mask))

    logits = torch.arange(max_len).float()
    mask = []
    for i in lengths.tolist():
        mask.append([0 for _ in range(i)] + [1 for _ in range(max_len-i)])
    mask = torch.LongTensor(mask).bool()
    logits = logits.mul_(-1).unsqueeze(0).expand_as(tokens).contiguous().masked_fill_(mask, -float('inf'))
    probs = torch.softmax(logits.mul_(tau), dim=-1)
    num_words = torch.distributions.Categorical(probs).sample().float()
    lengths = lengths.float()

    # sample the indices to corrupt
    corrupt_pos = num_words.div_(lengths).unsqueeze(1).expand_as(tokens).contiguous().masked_fill_(sample_mask, 0)
    corrupt_pos = torch.bernoulli(corrupt_pos, out=corrupt_pos).byte().bool()
    total_words = int(corrupt_pos.sum())
    if total_words == 0:
        return tokens
    # sample the corrupts
    corrupt_val = torch.LongTensor(total_words)
    corrupts = torch.zeros_like(tokens).long()
    corrupts = corrupts.masked_scatter_(corrupt_pos, corrupt_val)
    sampled_tokens = tokens.add(corrupts).remainder_(len(dic)).masked_fill_(pad_mask, dic.pad())

    return sampled_tokens


def infer_language_pair(path):
    """Infer language pair from filename: <split>.<lang1>-<lang2>.(...).idx"""
    src, dst = None, None
    for filename in os.listdir(path):
        parts = filename.split('.')
        if len(parts) >= 3 and len(parts[1].split('-')) == 2:
            return parts[1].split('-')
    return src, dst

def add_tag(samples, key, tag):
    """ add tag for a list of samples  """
    for i, sample in enumerate(samples):
        orig_data = samples[i][key]
        samples[i][key] = torch.cat([torch.tensor([tag], dtype=orig_data.dtype, device=orig_data.device), orig_data])

def collate_tokens(values, pad_idx, eos_idx=None, left_pad=False, move_eos_to_beginning=False):
    """Convert a list of 1d tensors into a padded 2d tensor."""
    size = max(v.size(0) for v in values)
    res = values[0].new(len(values), size).fill_(pad_idx)

    def copy_tensor(src, dst):
        assert dst.numel() == src.numel()
        if move_eos_to_beginning:
            assert src[-1] == eos_idx
            dst[0] = eos_idx
            dst[1:] = src[:-1]
        else:
            dst.copy_(src)

    for i, v in enumerate(values):
        copy_tensor(v, res[i][size - len(v):] if left_pad else res[i][:len(v)])
    return res


def load_indexed_dataset(path, dictionary, dataset_impl=None, combine=False, default='cached'):
    """A helper function for loading indexed datasets.

    Args:
        path (str): path to indexed dataset (e.g., 'data-bin/train')
        dictionary (~fairseq.data.Dictionary): data dictionary
        dataset_impl (str, optional): which dataset implementation to use. If
            not provided, it will be inferred automatically. For legacy indexed
            data we use the 'cached' implementation by default.
        combine (bool, optional): automatically load and combine multiple
            datasets. For example, if *path* is 'data-bin/train', then we will
            combine 'data-bin/train', 'data-bin/train1', ... and return a
            single ConcatDataset instance.
    """
    from fairseq.data.concat_dataset import ConcatDataset
    import fairseq.data.indexed_dataset as indexed_dataset

    datasets = []
    for k in itertools.count():
        path_k = path + (str(k) if k > 0 else '')

        dataset_impl_k = dataset_impl
        if dataset_impl_k is None:
            dataset_impl_k = indexed_dataset.infer_dataset_impl(path_k)

        dataset = indexed_dataset.make_dataset(
            path_k,
            impl=dataset_impl_k or default,
            fix_lua_indexing=True,
            dictionary=dictionary,
        )
        if dataset is None:
            break
        print('| loaded {} examples from: {}'.format(len(dataset), path_k))
        datasets.append(dataset)
        if not combine:
            break
    if len(datasets) == 0:
        return None
    elif len(datasets) == 1:
        return datasets[0]
    else:
        return ConcatDataset(datasets)


@contextlib.contextmanager
def numpy_seed(seed, *addl_seeds):
    """Context manager which seeds the NumPy PRNG with the specified seed and
    restores the state afterward"""
    if seed is None:
        yield
        return
    if len(addl_seeds) > 0:
        seed = int(hash((seed, *addl_seeds)) % 1e6)
    state = np.random.get_state()
    np.random.seed(seed)
    try:
        yield
    finally:
        np.random.set_state(state)


def collect_filtered(function, iterable, filtered, noskip=False):
    """
    Similar to :func:`filter` but collects filtered elements in ``filtered``.

    Args:
        function (callable): function that returns ``False`` for elements that
            should be filtered
        iterable (iterable): iterable to filter
        filtered (list): list to store filtered elements
    """
    for el in iterable:
        if function(el) or noskip:
            yield el
        else:
            filtered.append(el)


def _filter_by_size_dynamic(indices, size_fn, max_positions, raise_exception=False, noskip=False):
    def check_size(idx):
        if isinstance(max_positions, float) or isinstance(max_positions, int):
            s = size_fn(idx)
            if not (isinstance(s, float) or isinstance(s, int)):
                return list(s.values())[0] <= max_positions
            return size_fn(idx) <= max_positions
        elif isinstance(max_positions, dict):
            idx_size = size_fn(idx)
            assert isinstance(idx_size, dict)
            intersect_keys = set(max_positions.keys()) & set(idx_size.keys())
            return all(
                all(a is None or b is None or a <= b
                    for a, b in zip(idx_size[key], max_positions[key]))
                for key in intersect_keys
            )
        else:
            # Hacky as heck, for the specific case of multilingual training with RoundRobin.
            if isinstance(size_fn(idx), dict) and isinstance(max_positions, tuple):
                return all(
                    a is None or b is None or a <= b
                    for a, b in zip(size_fn(idx).values(), max_positions)
                )
            # For MultiCorpusSampledDataset, will generalize it later
            if not isinstance(size_fn(idx), Iterable):
                return all(size_fn(idx) <= b for b in max_positions)
            return all(
                a is None or b is None or a <= b
                for a, b in zip(size_fn(idx), max_positions)
            )
    ignored = []
    itr = collect_filtered(check_size, indices, ignored, noskip=noskip)
    if noskip:
        ignored = []
    indices = np.fromiter(itr, dtype=np.int64, count=-1)
    return indices, ignored


def filter_by_size(indices, dataset, max_positions, raise_exception=False, noskip=False):
    """
    Filter indices based on their size.

    Args:
        indices (List[int]): ordered list of dataset indices
        dataset (FairseqDataset): fairseq dataset instance
        max_positions (tuple): filter elements larger than this size.
            Comparisons are done component-wise.
        raise_exception (bool, optional): if ``True``, raise an exception if
            any elements are filtered (default: False).
    """
    if isinstance(max_positions, float) or isinstance(max_positions, int):
        if hasattr(dataset, 'sizes') and isinstance(dataset.sizes, np.ndarray):
            ignored = indices[dataset.sizes[indices] > max_positions].tolist()
            indices = indices[dataset.sizes[indices] <= max_positions]
        elif hasattr(dataset, 'sizes') and isinstance(dataset.sizes, list) and len(dataset.sizes) == 1:
            ignored = indices[dataset.sizes[0][indices] > max_positions].tolist()
            indices = indices[dataset.sizes[0][indices] <= max_positions]
        else:
            indices, ignored = _filter_by_size_dynamic(indices, dataset.size, max_positions, noskip=noskip)
    else:
        indices, ignored = _filter_by_size_dynamic(indices, dataset.size, max_positions, noskip=noskip)

    if len(ignored) > 0 and raise_exception:
        raise Exception((
            'Size of sample #{} is invalid (={}) since max_positions={}, '
            'skip this example with --skip-invalid-size-inputs-valid-test'
        ).format(ignored[0], dataset.size(ignored[0]), max_positions))
    if len(ignored) > 0:
        print((
            '| WARNING: {} samples have invalid sizes and will be skipped, '
            'max_positions={}, first few sample ids={}'
        ).format(len(ignored), max_positions, ignored[:10]))
    return indices

def filter_by_data_actor(indices, dataset, data_actor, data_filter_percentage=-1, trainer=None):
    """
    Filter indices based on their size.

    Args:
        indices (List[int]): ordered list of dataset indices
        dataset (FairseqDataset): fairseq dataset instance
        max_positions (tuple): filter elements larger than this size.
            Comparisons are done component-wise.
        raise_exception (bool, optional): if ``True``, raise an exception if
            any elements are filtered (default: False).
    """
    bins = 50
    if trainer.args.random_data_filter:
        orig_data_size = len(indices)
        indices = np.array(indices)
        np.random.shuffle(indices)

        #interval = int(len(indices)/bins)
        #start_idx, end_idx, numfiltered = 0, 0, 0
        #while end_idx < len(indices):
        #    end_idx = min(len(indices), start_idx + interval)
        #    current_indices = indices[start_idx:end_idx]
        #    numfiltered += int(len(current_indices)*data_filter_percentage)
        #    start_idx = end_idx

        #indices = indices[numfiltered:]
        indices = indices[int(len(indices)*data_filter_percentage):]
        print("Orignial data size={}; filtered data size={}".format(orig_data_size, len(indices)))
        indices.sort()
        return indices
    elif trainer.args.random_data_filter_by_len:
        orig_data_size = len(indices)
        indices = np.array(indices)
        selected = []
        interval = int(len(indices)/bins)
        start_idx, end_idx = 0, 0
        while end_idx < len(indices):
            end_idx = min(len(indices), start_idx + interval)
            current_indices = indices[start_idx:end_idx]
            np.random.shuffle(current_indices)
            selected.extend(current_indices[int(len(current_indices)*data_filter_percentage):].tolist())
            start_idx = end_idx
        indices = np.array(selected)
        indices.sort()
        print("Orignial data size={}; filtered data size={}".format(orig_data_size, len(indices)))
        return indices
    else:
        # calculate data actor score
        # create mini-batches with given size constraints
        max_tokens = 4800
        max_sentences = 100
        batch_sampler = batch_by_size(
            indices, dataset.num_tokens, max_tokens=max_tokens, max_sentences=max_sentences,
        )
        # return a reusable, sharded iterator
        itr = iterators.EpochBatchIterator(
            dataset=dataset,
            collate_fn=dataset.collater,
            batch_sampler=batch_sampler
        ).next_epoch_itr(shuffle=False)
        idx_start, idx_end = 0, 0
        scores = np.zeros(len(indices))
        ids = np.zeros(len(indices), dtype=np.int64)
        for i, sample in enumerate(itr):
            sample = trainer._prepare_sample(sample)
            sample = list(sample.values())[0]
            #print(sample)
            score = data_actor(sample['net_input']['src_tokens'], sample['target']).data.cpu().numpy()
            idx_start = idx_end
            idx_end = idx_start + score.shape[0]
            scores[idx_start:idx_end] = score.ravel()
            ids[idx_start:idx_end] = sample['id'].data.cpu().numpy().ravel()
        # argsort is ascending order
        preserved_indices = np.argsort(scores)[int(len(indices)*data_filter_percentage):]
        indices = np.array(ids)[preserved_indices]

        #score_indices = np.argsort(scores)
        #selected = []
        #interval = int(len(scores)/bins)
        #start_idx, end_idx = 0, 0
        #while end_idx < len(score_indices):
        #    end_idx = min(len(scores), start_idx + interval)
        #    current_indices = score_indices[start_idx:end_idx]
        #    np.random.shuffle(current_indices)
        #    selected.extend(current_indices[int(len(current_indices)*data_filter_percentage):].tolist())
        #    start_idx = end_idx
        #indices = np.array(selected)
        indices.sort()
        print("Orignial data size={}; filtered data size={}".format(len(ids), len(indices)))
        return indices


def batch_by_size(
    indices, num_tokens_fn, max_tokens=None, max_sentences=None,
    required_batch_size_multiple=1,
):
    """
    Yield mini-batches of indices bucketed by size. Batches may contain
    sequences of different lengths.

    Args:
        indices (List[int]): ordered list of dataset indices
        num_tokens_fn (callable): function that returns the number of tokens at
            a given index
        max_tokens (int, optional): max number of tokens in each batch
            (default: None).
        max_sentences (int, optional): max number of sentences in each
            batch (default: None).
        required_batch_size_multiple (int, optional): require batch size to
            be a multiple of N (default: 1).
    """
    try:
        from fairseq.data.data_utils_fast import batch_by_size_fast
    except ImportError:
        raise ImportError(
            'Please build Cython components with: `pip install --editable .` '
            'or `python setup.py build_ext --inplace`'
        )

    max_tokens = max_tokens if max_tokens is not None else sys.maxsize
    max_sentences = max_sentences if max_sentences is not None else sys.maxsize
    bsz_mult = required_batch_size_multiple

    if isinstance(indices, types.GeneratorType):
        indices = np.fromiter(indices, dtype=np.int64, count=-1)

    return batch_by_size_fast(indices, num_tokens_fn, max_tokens, max_sentences, bsz_mult)


def process_bpe_symbol(sentence: str, bpe_symbol: str):
    if bpe_symbol == 'sentencepiece':
        sentence = sentence.replace(' ', '').replace('\u2581', ' ').strip()
    elif bpe_symbol is not None:
        sentence = (sentence + ' ').replace(bpe_symbol, '').rstrip()
    return sentence
