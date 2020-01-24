# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from collections import OrderedDict
import os
import numpy as np

import torch

from fairseq import options, utils
from fairseq.data import (
    Dictionary,
    LanguagePairDataset,
    RoundRobinZipDatasets,
    TransformEosLangPairDataset,
    MultiCorpusSampledDataset,
    TCSSampledDataset,
)
from fairseq.models import FairseqMultiModel
from fairseq.tasks.translation import load_langpair_dataset


from . import FairseqTask, register_task


def _lang_token(lang: str):
    return '__{}__'.format(lang)


def _lang_token_index(dic: Dictionary, lang: str):
    """Return language token index."""
    idx = dic.index(_lang_token(lang))
    assert idx != dic.unk_index, \
        'cannot find language token for lang {}'.format(lang)
    return idx


@register_task('multilingual_translation')
class MultilingualTranslationTask(FairseqTask):
    """A task for training multiple translation models simultaneously.

    We iterate round-robin over batches from multiple language pairs, ordered
    according to the `--lang-pairs` argument.

    The training loop is roughly:

        for i in range(len(epoch)):
            for lang_pair in args.lang_pairs:
                batch = next_batch_for_lang_pair(lang_pair)
                loss = criterion(model_for_lang_pair(lang_pair), batch)
                loss.backward()
            optimizer.step()

    In practice, `next_batch_for_lang_pair` is abstracted in a FairseqDataset
    (e.g., `RoundRobinZipDatasets`) and `model_for_lang_pair` is a model that
    implements the `FairseqMultiModel` interface.

    During inference it is required to specify a single `--source-lang` and
    `--target-lang`, which indicates the inference langauge direction.
    `--lang-pairs`, `--encoder-langtok`, `--decoder-langtok` have to be set to
    the same value as training.
    """

    @staticmethod
    def add_args(parser):
        """Add task-specific arguments to the parser."""
        # fmt: off
        parser.add_argument('data', metavar='DIR', help='path to data directory')
        parser.add_argument('--lang-pairs', default=None, metavar='PAIRS',
                            help='comma-separated list of language pairs (in training order): en-de,en-fr,de-fr')
        parser.add_argument('-s', '--source-lang', default=None, metavar='SRC',
                            help='source language (only needed for inference)')
        parser.add_argument('-t', '--target-lang', default=None, metavar='TARGET',
                            help='target language (only needed for inference)')
        parser.add_argument('--encoder-lang-group', default=None, type=str,
                            help='source language group')
        parser.add_argument('--decoder-lang-group', default=None, type=str,
                            help='source language group')

        parser.add_argument('--lazy-load', action='store_true',
                            help='load the dataset lazily')
        parser.add_argument('--raw-text', default=False, action='store_true',
                            help='load raw text dataset')
        parser.add_argument('--left-pad-source', default='True', type=str, metavar='BOOL',
                            help='pad the source on the left (default: True)')
        parser.add_argument('--left-pad-target', default='False', type=str, metavar='BOOL',
                            help='pad the target on the left (default: False)')
        parser.add_argument('--max-source-positions', default=1024, type=int, metavar='N',
                            help='max number of tokens in the source sequence')
        parser.add_argument('--max-target-positions', default=1024, type=int, metavar='N',
                            help='max number of tokens in the target sequence')
        parser.add_argument('--upsample-primary', default=1, type=int,
                            help='amount to upsample primary dataset')
        parser.add_argument('--encoder-langtok', default=None, type=str, choices=['src', 'tgt'],
                            metavar='SRCTGT',
                            help='replace beginning-of-sentence in source sentence with source or target '
                                 'language token. (src/tgt)')
        parser.add_argument('--decoder-langtok', action='store_true',
                            help='replace beginning-of-sentence in target sentence with target language token')
        parser.add_argument('--dataset-type', default="round_robin", type=str,
                            help='[round_robin|multi|tcs]')

        # fmt: on

    def __init__(self, args, dicts, training):
        super().__init__(args)
        self.dataset_type = args.dataset_type
        self.dicts = dicts
        self.training = training
        if training:
            self.lang_pairs = args.lang_pairs
            args.source_lang, args.target_lang = args.lang_pairs[0].split('-')
        else:
            self.lang_pairs = ['{}-{}'.format(args.source_lang, args.target_lang)]
        if args.encoder_lang_group:
            encoder_lang_group = args.encoder_lang_group.split(",")
            args.encoder_lang_group = [lg.split('-') for lg in encoder_lang_group]
        if args.decoder_lang_group:
            decoder_lang_group = args.decoder_lang_group.split(",")
            args.decoder_lang_group = [lg.split('-') for lg in decoder_lang_group]

        if args.lan_dists is not None:
            args.lan_dists = np.array([np.exp(float(t)/1000) for t in args.lan_dists.split(',')])
            args.lan_dists = args.lan_dists/np.sum(args.lan_dists)
        # eval_lang_pairs for multilingual translation is usually all of the
        # lang_pairs. However for other multitask settings or when we want to
        # optimize for certain languages we want to use a different subset. Thus
        # the eval_lang_pairs class variable is provided for classes that extend
        # this class.
        self.eval_lang_pairs = args.eval_lang_pairs
        # model_lang_pairs will be used to build encoder-decoder model pairs in
        # models.build_model(). This allows multitask type of sub-class can
        # build models other than the input lang_pairs
        self.model_lang_pairs = self.lang_pairs
        self.langpair2id = {l: i for i, l in enumerate(self.model_lang_pairs)}
        self.langs = list(dicts.keys())

    @classmethod
    def setup_task(cls, args, **kwargs):
        dicts, training = cls.prepare(args, **kwargs)
        return cls(args, dicts, training)

    @classmethod
    def prepare(cls, args, **kargs):
        args.left_pad_source = options.eval_bool(args.left_pad_source)
        args.left_pad_target = options.eval_bool(args.left_pad_target)
        if getattr(args, 'raw_text', False):
            utils.deprecation_warning('--raw-text is deprecated, please use --dataset-impl=raw')
            args.dataset_impl = 'raw'
        elif getattr(args, 'lazy_load', False):
            utils.deprecation_warning('--lazy-load is deprecated, please use --dataset-impl=lazy')
            args.dataset_impl = 'lazy'

        if args.lang_pairs is None:
            raise ValueError('--lang-pairs is required. List all the language pairs in the training objective.')
        args.lang_pairs = args.lang_pairs.split(',')
        if args.eval_lang_pairs is not None:
            args.eval_lang_pairs = args.eval_lang_pairs.split(',')
        else:
            args.eval_lang_pairs = args.lang_pairs
        sorted_langs = sorted(list({x for lang_pair in args.lang_pairs for x in lang_pair.split('-')}))
        if args.source_lang is not None or args.target_lang is not None:
            training = False
        else:
            training = True

        # load dictionaries
        dicts = OrderedDict()
        for lang in sorted_langs:
            paths = args.data.split(':')
            assert len(paths) > 0
            dicts[lang] = Dictionary.load(os.path.join(paths[0], 'dict.{}.txt'.format(lang)))
            if len(dicts) > 0:
                assert dicts[lang].pad() == dicts[sorted_langs[0]].pad()
                assert dicts[lang].eos() == dicts[sorted_langs[0]].eos()
                assert dicts[lang].unk() == dicts[sorted_langs[0]].unk()
            if args.encoder_langtok is not None or args.decoder_langtok:
                for lang_to_add in sorted_langs:
                    dicts[lang].add_symbol(_lang_token(lang_to_add))
            print('| [{}] dictionary: {} types'.format(lang, len(dicts[lang])))
        return dicts, training

    def get_encoder_langtok(self, src_lang, tgt_lang):
        if self.args.encoder_langtok is None:
            return self.dicts[src_lang].eos()
        if self.args.encoder_langtok == 'src':
            return _lang_token_index(self.dicts[src_lang], src_lang)
        else:
            return _lang_token_index(self.dicts[src_lang], tgt_lang)

    def get_decoder_langtok(self, tgt_lang):
        if not self.args.decoder_langtok:
            return self.dicts[tgt_lang].eos()
        return _lang_token_index(self.dicts[tgt_lang], tgt_lang)

    def alter_dataset_langtok(self, lang_pair_dataset,
                              src_eos=None, src_lang=None, tgt_eos=None, tgt_lang=None,
                              tgt_langs=[], split='train'):
        if self.args.encoder_langtok is None and not self.args.decoder_langtok:
            return lang_pair_dataset

        new_src_eos = None
        if self.args.encoder_langtok is not None and src_eos is not None \
           and src_lang is not None and tgt_lang is not None:
            new_src_eos = self.get_encoder_langtok(src_lang, tgt_lang)
        else:
            src_eos = None

        new_tgt_bos = None
        if self.args.decoder_langtok and tgt_eos is not None and tgt_lang is not None:
            new_tgt_bos = self.get_decoder_langtok(tgt_lang)
        else:
            tgt_eos = None

        if split == 'train' and tgt_lang in tgt_langs:
            cur_tgt_idx = tgt_langs.index(tgt_lang)
            p = self.args.sample_tag_prob / (len(tgt_langs)-1)
            new_src_eos_list_probs = [p for _ in range(len(tgt_langs))]
            new_src_eos_list_probs[cur_tgt_idx] = 1-self.args.sample_tag_prob
            new_src_eos_list = [self.get_encoder_langtok(src_lang, t) for t in tgt_langs]
        else:
            new_src_eos_list = None
            new_src_eos_list_probs = None

        return TransformEosLangPairDataset(
            lang_pair_dataset,
            src_eos=src_eos,
            new_src_eos=new_src_eos,
            tgt_bos=tgt_eos,
            new_tgt_bos=new_tgt_bos,
            new_src_eos_list=new_src_eos_list,
            new_src_eos_list_probs=new_src_eos_list_probs,
            split=split,
        )

    def load_dataset(self, split, epoch=0, source_lang=None, target_lang=None, **kwargs):
        """Load a dataset split."""

        paths = self.args.data.split(':')
        assert len(paths) > 0
        data_path = paths[epoch % len(paths)]

        tgt_langs = []
        if self.args.sample_tag_prob > 0:
            for lang_pair in self.lang_pairs:
                src, tgt = lang_pair.split('-')
                tgt_langs.append(tgt)
        def language_pair_dataset(lang_pair):
            src, tgt = lang_pair.split('-')
            if tgt == 'turtoaze':
                src_tau = 0.5
                print('setting src_tau for to {}'.format(lang_pair, src_tau))
            else:
                src_tau = -1
            langpair_dataset = load_langpair_dataset(
                data_path, split, src, self.dicts[src], tgt, self.dicts[tgt],
                combine=True, dataset_impl=self.args.dataset_impl,
                upsample_primary=self.args.upsample_primary,
                left_pad_source=self.args.left_pad_source,
                left_pad_target=self.args.left_pad_target,
                max_source_positions=self.args.max_source_positions,
                max_target_positions=self.args.max_target_positions,
                src_tau=src_tau,
            )
            return self.alter_dataset_langtok(
                langpair_dataset,
                src_eos=self.dicts[tgt].eos(),
                src_lang=src,
                tgt_lang=tgt,
                tgt_langs=tgt_langs,
                split=split,
            )
        if split == 'valid':
            lang_pairs = self.eval_lang_pairs
        else:
            lang_pairs = self.lang_pairs

        if self.dataset_type == 'round_robin' or split != 'train':
            if source_lang is not None and target_lang is not None:
                training = False
            else:
                training = self.training
            if source_lang is None:
                source_lang = self.args.source_lang
            if target_lang is None:
                target_lang = self.args.target_lang
            self.datasets[split] = RoundRobinZipDatasets(
                OrderedDict([
                    (lang_pair, language_pair_dataset(lang_pair))
                    for lang_pair in lang_pairs
                ]),
                eval_key=None if training else "%s-%s" % (source_lang, target_lang),
            )
        elif self.dataset_type == 'multi':
            self.datasets[split] =  MultiCorpusSampledDataset(
                OrderedDict([
                    (lang_pair, language_pair_dataset(lang_pair))
                    for lang_pair in lang_pairs
                ]),
                sample_instance=self.args.sample_instance,
                split=split,
                datasize_t=self.args.datasize_t,
                alpha_p=self.args.alpha_p,
            )
        elif self.dataset_type == 'tcs':
            self.datasets[split] =  TCSSampledDataset(
                OrderedDict([
                    (lang_pair, language_pair_dataset(lang_pair))
                    for lang_pair in lang_pairs
                ]),
                lan_dists=self.args.lan_dists,
                data_condition=self.args.data_condition,
                sample_instance=self.args.sample_instance,
                split=split,
            )

    def build_dataset_for_inference(self, src_tokens, src_lengths):
        lang_pair = "%s-%s" % (self.args.source_lang, self.args.target_lang)
        return RoundRobinZipDatasets(
            OrderedDict([(
                lang_pair,
                self.alter_dataset_langtok(
                    LanguagePairDataset(
                        src_tokens, src_lengths,
                        self.source_dictionary
                    ),
                    src_eos=self.source_dictionary.eos(),
                    src_lang=self.args.source_lang,
                    tgt_lang=self.args.target_lang,
                ),
            )]),
            eval_key=lang_pair,
        )

    def build_model(self, args):
        def check_args():
            messages = []
            if len(set(self.args.lang_pairs).symmetric_difference(args.lang_pairs)) != 0:
                messages.append('--lang-pairs should include all the language pairs {}.'.format(args.lang_pairs))
            if self.args.encoder_langtok != args.encoder_langtok:
                messages.append('--encoder-langtok should be {}.'.format(args.encoder_langtok))
            if self.args.decoder_langtok != args.decoder_langtok:
                messages.append('--decoder-langtok should {} be set.'.format("" if args.decoder_langtok else "not"))

            if len(messages) > 0:
                raise ValueError(' '.join(messages))

        # Check if task args are consistant with model args
        check_args()

        from fairseq import models
        model = models.build_model(args, self)
        if not isinstance(model, FairseqMultiModel):
            raise ValueError('MultilingualTranslationTask requires a FairseqMultiModel architecture')
        return model

    def train_step(self, sample, model, criterion, optimizer, ignore_grad=False, data_actor=None, loss_copy=None, data_actor_out=None):
        model.train()
        agg_loss, agg_sample_size, agg_logging_output = 0., 0., {}
        normed_data_score = {}
        if (self.args.data_actor_step_update) and data_actor is not None:
            data_score, sum_score, example_size = {}, 0, 0
            for lang_pair in self.model_lang_pairs:
                if lang_pair not in sample or sample[lang_pair] is None or len(sample[lang_pair]) == 0:
                    continue
                cur_sample = sample[lang_pair]
                score = data_actor(cur_sample)
                data_actor_out[lang_pair] = score
                data_score[lang_pair] = score
                sum_score += score.sum()
                example_size += cur_sample['nsentences']
            # normalize scores
            for lang_pair in self.model_lang_pairs:
                if lang_pair not in sample or sample[lang_pair] is None or len(sample[lang_pair]) == 0:
                    continue
                #if self.args.out_score_type == 'exp':
                #    data_actor_out[lang_pair] = data_actor_out[lang_pair]/sum_score
                if self.args.out_score_type == 'tanh':
                    normed_data_score[lang_pair] = torch.softmax(data_score[lang_pair], dim=0) * example_size
                else:
                    normed_data_score[lang_pair] = data_score[lang_pair]*example_size/sum_score
                #print(data_score[lang_pair])
        else:
            data_score = None
        #print(sample)
        for lang_pair in self.model_lang_pairs:
            if lang_pair not in sample or sample[lang_pair] is None or len(sample[lang_pair]) == 0:
                continue
            # If we filer data, do not scale by score
            if data_score is not None and  self.args.select_by_dds_epoch < 0:
                score = normed_data_score[lang_pair]
            else:
                score = None
            loss, sample_size, logging_output, nll_loss_data = criterion(model.models[lang_pair], sample[lang_pair], data_score=score, loss_copy=(loss_copy is not None))
            if loss_copy is not None:
                loss_copy[lang_pair] = nll_loss_data
            if ignore_grad:
                loss *= 0
            else:
                if type(optimizer) == list:
                    for optim in optimizer[:-1]:
                        optim.backward(loss, retain_graph=True)
                    optimizer[-1].backward(loss)
                else:
                    optimizer.backward(loss)
            agg_loss += loss.detach().item()
            # TODO make summing of the sample sizes configurable
            agg_sample_size += sample_size
            agg_logging_output[lang_pair] = logging_output
        return agg_loss, agg_sample_size, agg_logging_output

    def valid_step(self, sample, model, criterion):
        model.eval()
        with torch.no_grad():
            agg_loss, agg_sample_size, agg_logging_output = 0., 0., {}
            for lang_pair in self.eval_lang_pairs:
                if lang_pair not in sample or sample[lang_pair] is None or len(sample[lang_pair]) == 0:
                    continue
                loss, sample_size, logging_output, _ = criterion(model.models[lang_pair], sample[lang_pair])
                agg_loss += loss.data.item()
                # TODO make summing of the sample sizes configurable
                agg_sample_size += sample_size
                agg_logging_output[lang_pair] = logging_output
        return agg_loss, agg_sample_size, agg_logging_output

    def inference_step(self, generator, models, sample, prefix_tokens=None):
        with torch.no_grad():
            return generator.generate(
                    models,
                    sample,
                    prefix_tokens=prefix_tokens,
                    bos_token=_lang_token_index(self.target_dictionary, self.args.target_lang)
                    if self.args.decoder_langtok else self.target_dictionary.eos(),
            )

    def init_logging_output(self, sample):
        return {
            'ntokens': sum(
                sample_lang.get('ntokens', 0)
                for sample_lang in sample.values()
            ) if sample is not None else 0,
            'nsentences': sum(
                sample_lang['target'].size(0) if 'target' in sample_lang else 0
                for sample_lang in sample.values()
            ) if sample is not None else 0,
        }

    def grad_denom(self, sample_sizes, criterion):
        return criterion.__class__.grad_denom(sample_sizes)

    def aggregate_logging_outputs(self, logging_outputs, criterion, logging_output_keys=None):
        logging_output_keys = logging_output_keys or self.lang_pairs
        # aggregate logging outputs for each language pair
        agg_logging_outputs = {
            key: criterion.__class__.aggregate_logging_outputs([
                logging_output.get(key, {}) for logging_output in logging_outputs
            ])
            for key in logging_output_keys
        }
        def sum_over_languages(key):
            return sum(logging_output[key] for logging_output in agg_logging_outputs.values())

        # flatten logging outputs
        flat_logging_output = {
            '{}:{}'.format(lang_pair, k): v
            for lang_pair, agg_logging_output in agg_logging_outputs.items()
            for k, v in agg_logging_output.items()
        }
        flat_logging_output['loss'] = sum_over_languages('loss')
        if any('nll_loss' in logging_output for logging_output in agg_logging_outputs.values()):
            flat_logging_output['nll_loss'] = sum_over_languages('nll_loss')
        flat_logging_output['sample_size'] = sum_over_languages('sample_size')
        flat_logging_output['nsentences'] = sum_over_languages('nsentences')
        flat_logging_output['ntokens'] = sum_over_languages('ntokens')
        return flat_logging_output

    @property
    def source_dictionary(self):
        return self.dicts[self.args.source_lang]

    @property
    def target_dictionary(self):
        return self.dicts[self.args.target_lang]

    def max_positions(self):
        """Return the max sentence length allowed by the task."""
        if len(self.datasets.values()) == 0:
            return {'%s-%s' % (self.args.source_lang, self.args.target_lang):
                    (self.args.max_source_positions, self.args.max_target_positions)}
        return OrderedDict([
            (key, (self.args.max_source_positions, self.args.max_target_positions))
            for split in self.datasets.keys()
            for key in self.datasets[split].datasets.keys()
        ])
