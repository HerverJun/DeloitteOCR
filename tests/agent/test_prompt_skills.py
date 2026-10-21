"""Prompt migration through local HTTP wire, real Skills and native data tools."""
import json
import unittest

import test_wire_context as wire_tests
from workbench_platform.agent.providers import SYSTEM, SYSTEM_PROMPTS


class PromptSkillTests(unittest.IsolatedAsyncioTestCase):
    setUp = wire_tests.WireContextTests.setUp
    tearDown = wire_tests.WireContextTests.tearDown
    asset = wire_tests.WireContextTests.asset
    execute = wire_tests.WireContextTests.execute

    async def test_ocr_discovery_activation_resource_and_explicit_tool_load(self):
        def choose(step, wire):
            system = wire['messages'][0]['content']
            names = [item['function']['name'] for item in wire['tools']]
            result = json.loads(next(message['content'] for message in reversed(wire['messages']) if message['role'] == 'tool')) if step else {}
            if step == 0:
                self.assertIn('没有适用技能', SYSTEM)
                self.assertNotIn('OCR可显式使用已有预处理参数', SYSTEM)
                self.assertNotIn('<registered_skill', system)
                return 'skills_list', {'keyword': 'invoice-ocr', 'limit': 1}
            if step == 1:
                self.assertEqual(result['data']['items'][0]['name'], 'invoice-ocr-reconciliation')
                return 'skills_activate', {'name': 'invoice-ocr-reconciliation'}
            if step == 2:
                self.assertIn('<registered_skill name="invoice-ocr-reconciliation"', system)
                self.assertIn('同图、同输入版本和同批次', system)
                return 'skills_read_resource', {'name': 'invoice-ocr-reconciliation', 'path': 'references/ocr-to-ledger.md'}
            if step == 3:
                self.assertEqual(result['data']['kind'], 'reference')
                self.assertEqual(result['data']['encoding'], 'utf-8')
                self.assertGreater(len(result['data']['content']), 0)
                self.assertNotIn('ocr_fuse_results', names)
                return 'tools_describe', {'names': ['ocr_list_processing_options', 'ocr_fuse_results']}
            self.assertIn('ocr_list_processing_options', names)
            self.assertIn('ocr_fuse_results', names)
            return 'conversation_answer', {'content': '已按需读取识别流程和结构桥资源，并加载已有处理与融合工具。'}
        wires, requests = await self.execute(choose, '使用票据识别核对技能，先了解流程和现有工具')
        self.assertEqual(len(wires), 5)
        self.assertEqual(requests[-1]['request_snapshot']['system_version'], 'clear-agent-system-v3')

    async def test_classification_guidance_keeps_training_and_model_switch_goal_bound(self):
        def choose(step, wire):
            system = wire['messages'][0]['content']
            result = json.loads(next(message['content'] for message in reversed(wire['messages']) if message['role'] == 'tool')) if step else {}
            if step == 0:
                self.assertNotIn('分类无模型时可导入', SYSTEM)
                self.assertIn('训练或切换专业模型须对应用户明确目标', SYSTEM)
                return 'skills_activate', {'name': 'classification-training-preparation'}
            if step == 1:
                self.assertIn('不能因项目缺少模型便默认发起训练', system)
                return 'skills_read_resource', {'name': 'classification-training-preparation', 'path': 'references/labels-and-splits.md'}
            self.assertEqual(result['data']['kind'], 'reference')
            return 'conversation_answer', {'content': '已读取分类准备规则；推理优先已有发布模型，训练和切换按明确目标执行。'}
        wires, _ = await self.execute(choose, '了解分类数据准备技能和标注规则，先不训练')
        self.assertEqual(len(wires), 3)
        self.assertIn('分类无模型时可导入', SYSTEM_PROMPTS['clear-agent-system-v1'])

    async def test_no_matching_skill_can_continue_with_real_registered_data_tools(self):
        asset = self.asset('suppliers-left.csv')
        def choose(step, wire):
            result = json.loads(next(message['content'] for message in reversed(wire['messages']) if message['role'] == 'tool')) if step else {}
            if step == 0:
                return 'skills_list', {'keyword': '无适用技能的基础读取', 'limit': 1}
            if step == 1:
                self.assertEqual(result['data']['total'], 0)
                return 'tools_describe', {'names': ['data_create_session', 'data_import', 'data_read_table']}
            if step == 2:
                return 'data_create_session', {'name': '无技能的基础读取'}
            if step == 3:
                return 'data_import', {'data_session_id': result['data']['data_session_id'],
                    'expected_version': result['data']['version'], 'side': 'left', 'asset_id': asset.asset_id,
                    'asset_revision': asset.revision, 'blob_hash': asset.blob_hash}
            if step == 4:
                self.assertEqual(result['status'], 'success')
                return 'data_read_table', {'data_session_id': result['data']['data_session_id'], 'side': 'left'}
            if step == 5:
                self.assertEqual(result['data']['rows'][0]['cells'][0]['value'], '000001')
                return 'delivery_publish', {'summary': '已调用注册工具导入并读取真实台账', 'result_refs': result['result_refs']}
            return None, {}
        wires, _ = await self.execute(choose, '不使用专用技能，调用已有工具导入台账并读取实际内容')
        self.assertEqual(len(wires), 6)
        self.assertNotIn('<registered_skill', '\n'.join(wire['messages'][0]['content'] for wire in wires))
        self.assertEqual(len(self.native.list('wtest')), 1)


if __name__ == '__main__':
    unittest.main()
