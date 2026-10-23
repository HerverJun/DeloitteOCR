import math
import pytest

from scripts.aigc_evaluation import (VARIANTS, binomial_upper, calibrate, metrics,
                                     ranking_metrics, summarize, validate)


def row(sample, split='test', domain='document', label='normal', score=.1, variant='original', **extra):
    return {'sample_id': sample, 'group_id': sample, 'split': split, 'domain': domain,
            'label': label, 'variant': variant, 'engine': 'fixture', 'status': 'succeeded',
            'signal_score': score, **extra}


def test_zero_false_positives_do_not_imply_zero_risk():
    assert binomial_upper(0, 100) == pytest.approx(1-.05**.01)
    assert binomial_upper(0, 100) > .01
    assert binomial_upper(0, 1000) < .01
    assert binomial_upper(10, 1000) > .01


def test_calibration_is_common_conservative_and_strict_with_ties():
    rows = [row(domain+str(i), 'calibration', domain, score=.1 if domain == 'document' else .3, variant=v)
            for domain in ('document', 'general') for i in range(10) for v in VARIANTS]
    frozen = calibrate(rows, 'fixture')
    assert frozen['threshold'] == .3
    assert frozen['comparison'] == 'strictly_greater'
    assert not frozen['qualified'] and not frozen['sample_requirements_met']
    assert metrics([row('at', score=.3), row('above', label='generated', score=.3001)], .3)['false_positives'] == 0


def test_failed_normal_is_unknown_and_blocks_low_false_positive_qualification():
    result = metrics([row('fail', status='failed', signal_score=None), row('real')], .5)
    assert result['normal_failed'] == 1
    assert result['conservative_fpr_including_unknown'] == .5
    assert result['normal_error_groups_including_unknown'] == 1
    assert result['fpr_upper_95'] > .5


def test_failed_positive_stays_in_recall_and_edits_are_separate():
    result = metrics([row('positive', label='generated', score=.9),
                      row('failed', label='generated', status='failed', signal_score=None),
                      row('edit', label='local_edit', score=.9)], .5)
    assert result['recall'] == .5
    assert result['generated'] == 2 and result['local_edit_signals'] == 1


@pytest.mark.parametrize('identity', ['group_id', 'original_sha256', 'prompt_family'])
def test_source_leakage_rejected(identity):
    rows = [row('a', 'development', **{identity:'same'}), row('b', 'test', **{identity:'same'})]
    with pytest.raises(ValueError, match='leaks'):
        validate(rows)


def test_duplicates_invalid_scores_and_positive_calibration_rejected():
    with pytest.raises(ValueError, match='Duplicate'):
        validate([row('a'), row('a')])
    with pytest.raises(ValueError, match='score'):
        validate([row('a', score=math.nan)])
    with pytest.raises(ValueError, match='normal sources'):
        calibrate([row('a', 'calibration', label='generated')], 'fixture')


def test_tied_ranking_is_chance_and_separation_is_perfect():
    tied = ranking_metrics([row('a', score=.5), row('b', label='generated', score=.5)])
    assert tied['auroc'] == .5 and tied['auprc'] == .5
    perfect = ranking_metrics([row('a'), row('b', label='generated', score=.9)])
    assert perfect['auroc'] == 1 and perfect['auprc'] == 1


def test_missing_variants_and_frozen_calibration_tampering():
    frozen = calibrate([row('a', 'calibration')], 'fixture')
    assert frozen['threshold'] is None and len(frozen['failed_groups']) == 1
    assert not summarize([], 'fixture', frozen)['qualified']
    frozen['threshold'] = .2
    with pytest.raises(ValueError, match='changed'):
        summarize([], 'fixture', frozen)


def test_generated_source_count_cannot_be_inflated_by_many_images():
    rows = [row(str(i), label='generated', score=.9, group_id='one-prompt-family') for i in range(800)]
    measured = metrics(rows,.5)
    assert measured['generated'] == 800
    assert measured['generated_source_groups'] == 1


def test_inconsistent_labels_across_variants_are_rejected():
    with pytest.raises(ValueError,match='Inconsistent'):
        validate([row('a'),row('a',variant='jpeg70',label='generated')])


def test_missing_holdout_variants_materialize_unknowns_and_model_identity_is_frozen():
    identity={'preprocessing_version':'fixture-recipe-v1','model_identity_sha256':'a'*64,
              'model_weight_hashes':{'weights.pth':'b'*64}}
    calibration_rows=[row('cal-'+domain,'calibration',domain,variant=v,**identity)
                      for domain in ('document','general') for v in VARIANTS]
    frozen=calibrate(calibration_rows,'fixture')
    assert frozen['model_weight_hashes']==identity['model_weight_hashes']
    report=summarize([*calibration_rows,row('test-real',score=.01,**identity)],'fixture',frozen)
    assert report['groups']['document/jpeg70']['failed_or_rejected']==1
    assert report['groups']['document/jpeg70']['normal']==1
    assert any('missing paired' in reason for reason in report['gaps'])
    with pytest.raises(ValueError,match='Holdout model'):
        summarize([*calibration_rows,row('test-swap',**{**identity,'model_identity_sha256':'c'*64})],'fixture',frozen)
