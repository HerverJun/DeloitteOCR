import { useEffect, useRef, useState } from "react";
import { Button, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Field, Input, Select, Spinner } from "@fluentui/react-components";
import { request } from "./api";
import type { AgentConnectionState } from "./agentTypes";

export function AgentConnection({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [saved, setSaved] = useState<AgentConnectionState | null>(null);
  const [protocol, setProtocol] = useState("openai_chat_completions");
  const [url, setUrl] = useState("");
  const [model, setModel] = useState("");
  const [key, setKey] = useState("");
  const [tokenParameter, setTokenParameter] = useState("max_completion_tokens");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const active = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController(); active.current = controller;
    void request("/agent/connection", { signal: controller.signal }).then(r => r.json()).then((value: AgentConnectionState) => {
      if (controller.signal.aborted) return;
      setSaved(value); setProtocol(value.protocol || "openai_chat_completions"); setUrl(value.base_url || ""); setModel(value.model || ""); setTokenParameter(value.token_parameter || "max_completion_tokens");
    }).catch(e => { if (!controller.signal.aborted) setError(String(e)); }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => { mounted.current = false; active.current?.abort(); };
  }, []);
  const run = async (clear = false) => {
    if (busy) return;
    setBusy(true); setError("");
    const controller = new AbortController(); active.current = controller;
    try {
      await request("/agent/connection", { method: clear ? "DELETE" : "PUT", signal: controller.signal,
        ...(!clear ? { body: JSON.stringify({ protocol, base_url: url, model, api_key: key, token_parameter: tokenParameter }) } : {}) });
      if (mounted.current) { setKey(""); onSaved(); onClose(); }
    } catch (e) { if (mounted.current && !controller.signal.aborted) setError(String(e)); }
    finally { if (mounted.current) setBusy(false); }
  };
  return <Dialog open onOpenChange={(_, data) => { if (!data.open && !busy) onClose(); }}><DialogSurface className="external-review-dialog"><DialogBody>
    <DialogTitle>配置文档助手主控</DialogTitle>
    <DialogContent className="external-review-fields">
      <p>主控负责组织工具和解释文字。视觉审校使用独立连接。测试会发送随机探针，不包含项目资料。</p>
      <Field label="工具协议"><Select value={protocol} disabled={busy} onChange={(_, d) => { setProtocol(d.value); setKey(""); }}><option value="openai_chat_completions">OpenAI 兼容 Chat Completions</option><option value="anthropic_messages">Anthropic Messages</option></Select></Field>
      <Field label="API URL"><Input value={url} disabled={busy} autoComplete="off" onChange={(_, d) => { setUrl(d.value); setKey(""); }} placeholder="https://api.example.com/v1" /></Field>
      <Field label="模型 ID"><Input value={model} disabled={busy} onChange={(_, d) => setModel(d.value)} placeholder="填写支持原生工具调用的模型" /></Field>
      <Field label="API Key" hint={saved?.available ? "同一地址和协议可留空保留 Key。" : "加密保存在本机；换 Windows 用户后需重新配置。"}><Input type="password" autoComplete="new-password" value={key} disabled={busy} onChange={(_, d) => setKey(d.value)} /></Field>
      {protocol === "openai_chat_completions" && <Field label="输出长度参数"><Select value={tokenParameter} disabled={busy} onChange={(_, d) => setTokenParameter(d.value)}><option value="max_completion_tokens">max_completion_tokens</option><option value="max_tokens">max_tokens</option></Select></Field>}
      {error && <p role="alert" className="error">{error}</p>}
      {busy && <Spinner size="small" label="正在读取或验证连接" />}
    </DialogContent>
    <DialogActions><Button disabled={busy || !saved?.configured} onClick={() => void run(true)}>清除连接</Button><Button disabled={busy} onClick={onClose}>取消</Button><Button appearance="primary" disabled={busy || !url.trim() || !model.trim()} onClick={() => void run()}>测试并保存</Button></DialogActions>
  </DialogBody></DialogSurface></Dialog>;
}
