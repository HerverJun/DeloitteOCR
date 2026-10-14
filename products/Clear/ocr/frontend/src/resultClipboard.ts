export async function copySavedResultText({ id, flush, currentId, requestText, writeText }: {
  id: string;
  flush: () => Promise<unknown>;
  currentId: () => string | undefined;
  requestText: (path: string) => Promise<Response>;
  writeText: (text: string) => Promise<void>;
}) {
  const checkTarget = () => {
    if (currentId() !== id) throw Error("复制期间结果已切换，请重新复制当前结果。");
  };
  await flush();
  checkTarget();
  const response = await requestText(`/results/${id}/text`);
  const type = response.headers.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase();
  if (type !== "text/plain") throw Error("复制接口未返回纯文本，剪贴板未修改。请重试。");
  const text = await response.text();
  checkTarget();
  await writeText(text);
}
