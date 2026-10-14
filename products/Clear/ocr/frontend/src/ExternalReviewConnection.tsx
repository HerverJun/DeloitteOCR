import { useEffect, useRef, useState } from "react";
import { Button, Combobox, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface,
  DialogTitle, Field, Input, Option, Select, Spinner } from "@fluentui/react-components";
import { request } from "./api";

type Connection = { configured: boolean; has_key: boolean; protocol?: string; base_url?: string;
  model?: string; model_id?: string; reason?: string };
type Model = { id: string; label: string };

export function ExternalReviewConnection({ onClose, onSaved }: {
  onClose: () => void; onSaved: (modelId: string | null) => Promise<void>;
}) {
  const [saved, setSaved] = useState<Connection | null>(null);
  const [protocol, setProtocol] = useState("openai");
  const [url, setUrl] = useState("");
  const [key, setKey] = useState("");
  const [model, setModel] = useState("");
  const [models, setModels] = useState<Model[]>([]);
  const [busy, setBusy] = useState("正在读取配置");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const active = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const locked = useRef(false);
  const canReuseKey = !!saved?.has_key && saved.protocol === protocol && saved.base_url === url.trim();
  const canConnect = !!saved && !!url.trim() && (!!key.trim() || canReuseKey);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    active.current = controller;
    void request("/multimodal/external", { signal: controller.signal }).then(r => r.json()).then((value: Connection) => {
      if (controller.signal.aborted) return;
      setSaved(value); setProtocol(value.protocol || "openai"); setUrl(value.base_url || ""); setModel(value.model || "");
      if (value.reason) setMessage(value.reason);
    }).catch(e => { if (!controller.signal.aborted) setError(String(e)); })
      .finally(() => { if (!controller.signal.aborted) setBusy(""); });
    return () => { mounted.current = false; active.current?.abort(); };
  }, []);

  const run = async (operation: "models" | "save" | "clear") => {
    if (locked.current || busy) return;
    locked.current = true;
    const controller = new AbortController(); active.current = controller;
    setBusy(operation === "models" ? "正在获取模型" : operation === "save" ? "正在验证图片识别并保存" : "正在清除连接");
    setError(""); setMessage("");
    try {
      const response = await request(`/multimodal/external${operation === "models" ? "/models" : ""}`, {
        method: operation === "models" ? "POST" : operation === "save" ? "PUT" : "DELETE",
        signal: controller.signal,
        ...(operation !== "clear" ? { body: JSON.stringify({ protocol, base_url: url, api_key: key, model }) } : {}),
      });
      const value = await response.json();
      if (!mounted.current) return;
      if (operation === "models") {
        setModels(value.models);
        setMessage(value.models.length ? `已获取 ${value.models.length} 个模型，请搜索或选择；也可直接填写模型 ID。` : "服务没有返回模型，请直接填写模型 ID。");
      } else {
        setKey("");
        await onSaved(operation === "save" ? value.model_id : null);
        if (mounted.current) onClose();
      }
    } catch (e) {
      if (mounted.current && !controller.signal.aborted) {
        // Provider bodies are sanitized by the backend; do not echo form values.
        setError(String(e));
        if (operation === "models") setMessage("仍可直接填写模型 ID，再测试并保存。");
      }
    } finally {
      locked.current = false;
      if (mounted.current) setBusy("");
    }
  };

  const filtered = models.filter(item => `${item.id} ${item.label}`.toLowerCase().includes(model.toLowerCase()));
  return <Dialog open onOpenChange={(_, data) => { if (!data.open && !busy) onClose(); }}>
    <DialogSurface className="external-review-dialog" aria-busy={!!busy}>
      <DialogBody>
        <DialogTitle>配置外部 API</DialogTitle>
        <DialogContent className="external-review-fields">
          <p>保存一套视觉审校连接，可使用外部服务或内网 API。</p>
          <Field label="协议">
            <Select value={protocol} disabled={!!busy} onChange={(_, data) => {
              setProtocol(data.value); setKey(""); setModels([]); setModel(""); setMessage("");
            }}>
              <option value="openai">OpenAI 兼容</option><option value="anthropic">Anthropic</option>
            </Select>
          </Field>
          <Field label="API URL" hint="填写服务地址，例如 https://api.example.com/v1">
            <Input value={url} disabled={!!busy} autoComplete="off" onChange={(_, data) => {
              setUrl(data.value); setKey(""); setModels([]); setMessage("");
            }} placeholder="https://api.example.com/v1" />
          </Field>
          <Field label="API Key" hint={canReuseKey ? "已保存，留空保留现有 Key。" : "Key 加密保存在本机，换电脑后需重新填写。"}>
            <Input type="password" value={key} disabled={!!busy} autoComplete="new-password"
              onChange={(_, data) => { setKey(data.value); setModels([]); }} placeholder={canReuseKey ? "已保存" : "输入 API Key"} />
          </Field>
          <div className="external-review-model-header"><span>选择或填写视觉模型</span>
            <Button size="small" disabledFocusable={!!busy} disabled={!!busy || !canConnect} onClick={() => void run("models")}>获取模型</Button>
          </div>
          <Field label="模型 ID">
            <Combobox freeform value={model} selectedOptions={model ? [model] : []} disabled={!!busy}
              placeholder="搜索模型，或直接输入模型 ID" onChange={e => setModel(e.target.value)}
              onOptionSelect={(_, data) => { if (data.optionValue) setModel(data.optionValue); }}>
              {filtered.map(item => <Option key={item.id} value={item.id} text={item.id}>{item.label}</Option>)}
            </Combobox>
          </Field>
          <p className="multimodal-footnote">测试会发送一张内置小图片，验证模型能看图并返回审校结果。不会使用项目文档。</p>
          {busy && <div role="status"><Spinner size="tiny" />{busy}…</div>}
          {message && <p role="status">{message}</p>}
          {error && <p className="inline-warning" role="alert">{error}</p>}
        </DialogContent>
        <DialogActions>
          {saved?.configured && <Button disabledFocusable={!!busy} disabled={!!busy} onClick={() => void run("clear")}>清除连接</Button>}
          <Button disabledFocusable={!!busy} disabled={!!busy} onClick={onClose}>取消</Button>
          <Button appearance="primary" disabledFocusable={!!busy} disabled={!!busy || !canConnect || !model.trim()} onClick={() => void run("save")}>测试并保存</Button>
        </DialogActions>
      </DialogBody>
    </DialogSurface>
  </Dialog>;
}
