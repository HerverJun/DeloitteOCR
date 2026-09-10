const fromHash = new URLSearchParams(location.hash.slice(1)).get("token");
if (fromHash) {
  sessionStorage.setItem("ocr-token", fromHash);
  history.replaceState(null, "", location.pathname);
}
const token = () => sessionStorage.getItem("ocr-token") || "";
export async function request(path: string, options: RequestInit = {}) {
  const response = await fetch("/api" + path, {
    ...options,
    headers: {
      Authorization: "Bearer " + token(),
      ...(!(options.body instanceof FormData) && options.body
        ? { "Content-Type": "application/json" }
        : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const error = await response
      .json()
      .catch(() => ({ message: response.statusText }));
    throw new Error(error.message || error.detail || "请求未完成");
  }
  return response;
}
export async function api<T = any>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  return (
    await request(path, {
      method,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  ).json();
}
export async function download(ids: string[], format: string) {
  const response = await request("/export", {
    method: "POST",
    body: JSON.stringify({ result_ids: ids, format }),
  });
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download =
    response.headers
      .get("Content-Disposition")
      ?.match(/filename="([^"]+)"/)?.[1] || "OCR-export";
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
