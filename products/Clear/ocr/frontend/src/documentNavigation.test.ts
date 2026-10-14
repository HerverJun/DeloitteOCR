import { describe, expect, it } from "vitest";
import { currentDocumentPage, documentPageOffset, type PageSelection } from "./documentNavigation";
import type { DocumentPage } from "./types";

const page = (number: number, document = "doc", rendered = true): DocumentPage => ({
  id: `${document}-page-${number}`, document_id: document, page_number: number,
  image_id: rendered ? `${document}-image-${number}` : null,
  active_version: rendered ? `${document}-version-${number}` : null,
  status: rendered ? "ready" : "pending", stage_status: null, render_dpi: 150,
});
const selection = (value: DocumentPage, fromImage = value.image_id || "", awaitingOpen = false): PageSelection =>
  ({ page: value, fromImage, awaitingOpen });

describe("document page navigation", () => {
  it("uses the workspace image after task or review navigation instead of a previously selected page", () => {
    const first = page(1), second = page(2);
    expect(currentDocumentPage("doc", second.image_id!, [first, second], selection(first))).toBe(second);
  });

  it("does not process an old document page while a different document is loading", () => {
    const first = page(1), other = page(42, "other");
    expect(currentDocumentPage("other", other.image_id!, [first], selection(first))).toBeNull();
    expect(currentDocumentPage("other", other.image_id!, [other], selection(first))).toBe(other);
  });

  it("keeps the active page as the action target when browsing a different batch of thumbnails", () => {
    const active = page(1);
    expect(currentDocumentPage("doc", active.image_id!, [page(31)], selection(active))).toBe(active);
    expect([1, 30, 31, 60, 61].map(documentPageOffset)).toEqual([0, 0, 30, 30, 60]);
  });

  it("clears the action target when the workspace switches to an independent image or no image", () => {
    const first = page(1);
    expect(currentDocumentPage("doc", "independent-image", [first], selection(first))).toBeNull();
    expect(currentDocumentPage("doc", "", [first], selection(first))).toBeNull();
  });

  it("retains an unrendered page for recovery but invalidates it after external navigation", () => {
    const first = page(1), pending = page(32, "doc", false);
    const requested = selection(pending, first.image_id!, true);
    expect(currentDocumentPage("doc", first.image_id!, [pending], requested)).toBe(pending);
    expect(currentDocumentPage("doc", "independent-image", [pending], requested)).toBeNull();
    expect(currentDocumentPage("other", first.image_id!, [pending], requested)).toBeNull();
  });
});
