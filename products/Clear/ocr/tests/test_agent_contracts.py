"""P0 adversarial boundary tests. No production tool dispatch is claimed."""
import unittest

from pydantic import ValidationError
from ocr_workbench.agent.contracts import (
    Event, RunState, ToolResult, validate_call_batch, validate_result_pairing,
    validate_tool_arguments,
)


class AgentContractTests(unittest.TestCase):
    def test_rejects_boolean_float_string_and_duplicate_pages(self):
        for pages in ([True], [1.0], ["1"], [0], [1, 1], list(range(1, 102))):
            with self.subTest(pages=pages[:3]), self.assertRaises(ValidationError):
                validate_tool_arguments("process_pages", {"document_id":"d", "page_numbers":pages})
        parsed=validate_tool_arguments("process_pages", {"document_id":"d", "page_numbers":[1,100]})
        self.assertFalse(parsed.force)

    def test_model_cannot_inject_server_authority_or_paths(self):
        for key in ("project_id", "credential_ref", "absolute_path", "operation_key", "authorization", "generation"):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                validate_tool_arguments("search_document", {"document_id":"d", "query":"金额", key:"forged"})
        with self.assertRaises(ValidationError):
            validate_tool_arguments("request_visual_review", {"result_id":"r","revision":0,"version_id":"v","target_ids":["t"],"visual_model_id":"https://evil.invalid"})

    def test_query_and_engine_bounds(self):
        for extra in ({"query":""}, {"query":"字"*201}, {"limit":51}, {"limit":True}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                validate_tool_arguments("search_document", {"document_id":"d", "query":"合计", **extra})
        for data in ({"mode":"ocr"}, {"mode":"native","engine":"paddle"}):
            with self.assertRaises(ValidationError):
                validate_tool_arguments("process_pages", {"document_id":"d","page_numbers":[1],**data})
        with self.assertRaises(ValidationError):
            validate_tool_arguments("run_ocr", {"version_ids":["v"],"engines":["a","a"]})

    def test_adopted_and_run_result_sources_are_unambiguous(self):
        for args in ({"source":"run_result"}, {"source":"adopted","result_id":"r"}):
            with self.assertRaises(ValidationError):
                validate_tool_arguments("read_page_result", {"page_id":"p", **args})
        self.assertEqual(validate_tool_arguments("read_page_result", {"page_id":"p"}).source,"adopted")
        validate_tool_arguments("read_page_result", {"page_id":"p","source":"run_result","result_id":"r"})

    def test_exports_require_one_versioned_source(self):
        good={"source":"explicit_results","format":"xlsx","results":[{"result_id":"r","revision":0,"version_id":"v"}]}
        self.assertEqual(validate_tool_arguments("export_results",good).partial_policy,"ask")
        for patch in ({"source_run_id":"run"}, {"results":[]}, {"results":[{"result_id":"r","version_id":"v"}]}, {"source":"run_results"}):
            with self.assertRaises(ValidationError): validate_tool_arguments("export_results",{**good,**patch})

    def test_whole_response_rejected_for_late_invalid_call(self):
        valid={"call_id":"1","tool":"search_document","arguments":{"document_id":"d","query":"q"}}
        for other in ({**valid}, {**valid,"call_id":"2","tool":"shell"}, {**valid,"call_id":"2","arguments":{"document_id":"d","query":42}}):
            with self.assertRaises(ValueError): validate_call_batch([valid,other],complete=True)
        with self.assertRaisesRegex(ValueError,"truncated_response"):
            validate_call_batch([valid],complete=False)
        with self.assertRaises(ValueError): validate_call_batch([],complete=True)

    def test_every_call_requires_one_ordered_result(self):
        calls=validate_call_batch([{"call_id":str(i),"tool":"get_workspace_context","arguments":{}} for i in (1,2)],complete=True)
        results=[{"call_id":str(i),"result":{"status":"success","summary":"已读"}} for i in (1,2)]
        validate_result_pairing(calls,results)
        for malformed in (results[::-1], results[:1], results+[results[0]]):
            with self.assertRaisesRegex(ValueError,"unpaired"): validate_result_pairing(calls,malformed)

    def test_queued_is_not_success_and_errors_are_explicit(self):
        job={"kind":"ocr","job_id":"j","operation_id":"o","ownership":"created","state":"queued"}
        with self.assertRaises(ValidationError): ToolResult(status="success",summary="完成",job_refs=[job])
        with self.assertRaises(ValidationError): ToolResult(status="error",summary="失败")
        ToolResult(status="error",summary="失败",error={"code":"business_failed","message":"任务失败","retryable":True,"required_action":"retry_explicitly"})

    def test_result_limit_counts_utf8_and_cursor_requires_truncation(self):
        with self.assertRaises(ValidationError): ToolResult(status="success",summary="",data={"text":"汉"*5500})
        with self.assertRaises(ValidationError): ToolResult(status="success",summary="",next_cursor="next")
        ToolResult(status="success",summary="部分读取",truncated=True,next_cursor="next")

    def test_run_outcome_and_event_generation_cannot_disagree(self):
        for status,outcome in (("running","success"),("completed",None)):
            with self.assertRaises(ValidationError): RunState(run_id="r",session_id="s",status=status,generation=1,outcome=outcome)
        RunState(run_id="r",session_id="s",status="completed",generation=1,outcome="partial")
        event={"session_id":"s","run_id":"r","seq":1,"generation":1,"type":"run_state","created":"2026-09-20T00:00:00Z","payload":{}}
        Event.model_validate(event)
        with self.assertRaises(ValidationError): Event.model_validate({**event,"generation":None})
        with self.assertRaises(ValidationError): Event.model_validate({**event,"seq":True})


if __name__=="__main__": unittest.main()
