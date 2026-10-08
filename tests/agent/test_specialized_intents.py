from types import SimpleNamespace
import pytest

from workbench_platform.agent.matching import match_goal, WORKFLOWS


@pytest.mark.parametrize('goal', [
    '将固定范围内的200张图片整批转Excel，每图一个xlsx。此次用于质量评估。',
    '请把这些图片转成Excel。完成后介绍疑点和评估结果。',
    '帮我将图片转Excel，然后交付ZIP。这是实际模型评估。',
])
def test_explicit_action_is_not_cancelled_by_later_evaluation_context(goal):
    result = match_goal(goal, SimpleNamespace(tools={name: object() for name in WORKFLOWS['screen_excel']['tools']}))
    assert result['match'] == 'single' and result['intent'] == 'action'


@pytest.mark.parametrize('goal', [
    '介绍如何将200张图片转Excel。',
    '帮我评估图片转Excel是否方便。',
    '请制定将图片转Excel的提升方案。',
    '我之前将图片转Excel，想了解平台是否能平替。',
])
def test_information_requests_do_not_auto_activate_execution(goal):
    result = match_goal(goal, SimpleNamespace(tools={name: object() for name in WORKFLOWS['screen_excel']['tools']}))
    assert result['intent'] == 'information' and result['auto_skill_candidate'] is None
