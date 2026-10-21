"""User language routes complete goals without weakening extra requirements."""
from workbench_platform.application_services.goal_routing import extraction_intent, extraction_with_analysis


def test_image_phrasing_is_not_a_command_whitelist():
    for goal in ['为我提取图片中的文字。', '帮我识别一下这张图片里的文字', '提取图片文字，谢谢',
                 '把这张图里的字抄出来', '麻烦将所选照片的全部文字转成文本并下载TXT', '请读出这张图片上的所有字']:
        assert extraction_intent(goal, ['image/png']) == ('image', {})


def test_pdf_pages_are_explicit_and_bounded():
    assert extraction_intent('请提取PDF第2、5页的文字，谢谢', ['application/pdf']) == ('pdf', {'page_numbers': [2, 5]})
    assert extraction_intent('读取这份PDF第2至4页原文', ['application/pdf']) == ('pdf', {'page_numbers': [2, 3, 4]})
    assert extraction_intent('提取PDF第2,5页文字', ['application/pdf']) == ('pdf', {'page_numbers': [2, 5]})
    assert extraction_intent('读取PDF全文', ['application/pdf']) is None


def test_compound_goals_and_constraints_are_never_swallowed():
    for goal in ['提取图片文字并比较金额', '提取图片文字，解释异常', '不需要提取图片文字',
                 '提取图片文字，使用glm', '提取图片文字并制表', '提取图片文字，仅右侧区域',
                 '提取PDF第2、5页并比较金额，保留精度', '提取图片文字并翻译成英文']:
        assert extraction_intent(goal, ['image/png']) is None
    assert extraction_intent('提取图片文字', ['image/png', 'application/pdf']) is None


def test_compound_preparation_keeps_explicit_scope_and_all_analysis():
    assert extraction_with_analysis('读取PDF第2页并与第5页金额比较，保留原文/编号/金额精度，有实质歧义先问我',
                                    ['application/pdf']) == ('pdf', {'page_numbers': [2, 5]})
    assert extraction_with_analysis('提取图片文字并比较金额', ['image/png']) == ('image', {})
    assert extraction_with_analysis('提取图片文字，仅右侧区域，再比较金额', ['image/png']) is None
    assert extraction_with_analysis('读取PDF第2、5页并比较金额，不要处理其它页，也不要四舍五入',
                                    ['application/pdf']) == ('pdf', {'page_numbers': [2, 5]})
    assert extraction_with_analysis('读取PDF第2页到第5页并比较金额', ['application/pdf']) == ('pdf', {'page_numbers': [2, 3, 4, 5]})
    assert extraction_with_analysis('读取 PDF 第 2 页并与第 5 页金额比较，不要处理其它页',
                                    ['application/pdf']) == ('pdf', {'page_numbers': [2, 5]})
