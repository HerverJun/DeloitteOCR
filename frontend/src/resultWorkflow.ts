import type { Photo, Task } from "./types";
export const reviewNames: Record<string, string> = {
  pending: "待校对",
  confirmed: "已确认",
  question: "有疑问",
};
export function adoptedResult(photo: Photo, tasks: Task[]): string | null {
  return (
    photo.selected_result ||
    tasks
      .filter(
        (t) =>
          t.image_id === photo.id && t.status === "succeeded" && t.kind !== "fusion" && t.kind !== "multimodal" && t.engine !== "reviewer" && t.result_id,
      )
      .at(-1)?.result_id ||
    null
  );
}
export function exportPhotos(
  photos: Photo[],
  selected: string[],
  scope: string,
  confirmedOnly: boolean,
) {
  return photos.filter(
    (p) =>
      (scope !== "selected" || selected.includes(p.id)) &&
      (!confirmedOnly || p.review_status === "confirmed"),
  );
}
