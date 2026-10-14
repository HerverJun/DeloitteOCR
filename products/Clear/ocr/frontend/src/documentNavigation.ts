import type { DocumentPage } from "./types";

export type ActiveDocumentPage = { documentId: string; pageNumber: number };
export type PageSelection = { page: DocumentPage; fromImage: string; awaitingOpen: boolean };

export const documentPageOffset = (pageNumber: number) => Math.floor((pageNumber - 1) / 30) * 30;

export function currentDocumentPage(
  documentId: string,
  activeImage: string,
  pages: DocumentPage[],
  selection: PageSelection | null,
): DocumentPage | null {
  const selected = selection?.page.document_id === documentId ? selection : null;
  // A requested, not-yet-open page remains selectable for render recovery only
  // while the workspace is still on the image from which it was requested.
  if (selected?.awaitingOpen && selected.fromImage === activeImage)
    return pages.find(page => page.id === selected.page.id) || selected.page;
  if (!activeImage) return null;
  return pages.find(page => page.document_id === documentId && page.image_id === activeImage) ||
    (selected?.page.image_id === activeImage ? selected.page : null);
}
