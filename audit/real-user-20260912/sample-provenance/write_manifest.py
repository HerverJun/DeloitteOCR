"""Record the manually reviewed public image selection; does not alter images."""
from pathlib import Path
import hashlib
import json
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = ROOT / "sample-provenance"
SAMPLES = ROOT / "samples"
PADDLE_REV = "2661c7c0ef5c613e8f93c6e93b2e052399f0f854"
EASY_REV = "363afb184047ce452e436f4224f3098422df872e"
OMNI_REV = "aa1ee96d106dbe53d0ae59474d75c6e6d9b53fec"

def github_item(filename, repository, revision, repository_path, kind, expected, license_note):
    return {
        "file": "samples/" + filename,
        "kind": kind,
        "source_repository": repository,
        "revision": revision,
        "source_url": f"https://raw.githubusercontent.com/{repository}/{revision}/{repository_path}",
        "source_page": f"https://github.com/{repository}/blob/{revision}/{repository_path}",
        "retrieval": "Downloaded from the official GitHub repository during this audit; raw content or GitHub Contents API base64.",
        "pixel_transformations": "none",
        "license_or_restrictions": license_note,
        "expected_content": expected,
        "visual_review": "Real source image/document, visually checked; no OCR prediction boxes or transcription overlays.",
    }

repo_note = "Repository LICENSE is Apache-2.0; image-specific third-party rights were not independently established. Keep this audit copy for local evaluation; this manifest does not grant redistribution/commercial rights to the underlying document."
items = [
    github_item("chinese_store.jpg", "JaidedAI/EasyOCR", EASY_REV, "examples/chinese.jpg", "real_bilingual_street_photo", {
        "description": "上海愚园路蓝色路牌实拍，中英文字，背景建筑与透视。",
        "anchors": ["愚园路", "Yuyuan Rd.", "西", "东", "315", "309", "W", "E"],
    }, repo_note),
    github_item("english_book_photo.jpg", "PaddlePaddle/PaddleOCR", PADDLE_REV, "tests/test_files/book.jpg", "real_english_book_photo", {
        "description": "真实打开书籍的两页照片，弯曲书脊、阴影、漫画和数学式，适合去弯曲/裁剪/布局测试。",
        "anchors": ["The disappearing sum", "WHICH FOUR DO I FANCY MOST?", "Veronica’s choices", "94", "95"],
    }, repo_note),
    github_item("pubmed_table.png", "PaddlePaddle/PaddleOCR", PADDLE_REV, "docs/datasets/images/table_PubTabNet_demo/PMC524509_007_00.png", "real_scientific_table", {
        "description": "PubTabNet/PMC524509 真实科学论文表格，503×98 原始低分辨率，包含分组表头、四条数据行和 ± 数值。",
        "group_headers": ["[³H]DA uptake", "DAT density"],
        "columns": ["Group", "N", "Vmax", "SD", "Km", "SD", "DAT", "SD"],
        "rows": [
            ["WT w Veh", "4", "516.1", "± 56.5", "56.7", "± 18.6", "10.6", "± 2.5"],
            ["D3 KO w Veh", "4", "212.4", "± 18.2", "10.8", "± 5.7", "10.8", "± 1.5"],
            ["WT w PPX", "6", "183.4", "± 34.3", "28.4", "± 18.5", "9.9", "± 3.0"],
            ["D3 KO w PPX", "6", "223.6", "± 36.4", "10.4", "± 10.9", "9.4", "± 2.6"],
        ],
        "reference_status": "Manually read from the source image for spot checks; not an independently adjudicated benchmark ground truth.",
    }, "PubTabNet requires CDLA-Permissive-1.0 per the linked PaddleOCR table dataset documentation; underlying publication attribution should be retained. Source: https://github.com/ibm-aur-nlp/PubTabNet ; license: https://cdla.io/permissive-1-0/ ."),
    github_item("document_formula.png", "PaddlePaddle/PaddleOCR", PADDLE_REV, "tests/test_files/doc_with_formula.png", "real_english_scientific_document", {
        "description": "英文学术论文第 3 页，双栏、公式 (4)–(14)、上下标及 PDF 原生彩色引用框；彩色引用框属于文档本身，不是 OCR 结果。",
        "anchors": ["The quantity δ is the BH duty cycle", "All results shown in this work correspond", "SMBH", "QLF"],
    }, repo_note),
]

cache_records = json.loads((PROVENANCE / "omnidoc-cache-records.json").read_text("utf-8"))
omni_expected = {
    "chinese_scanned_exam.png": {
        "description": "中文诗词练习整页扫描，有浅绿原生水印、填空横线和选择题。",
        "anchors": ["23 题乌江亭", "[唐]杜牧", "胜败兵家事不期", "[宋]李清照", "生当作人杰，死亦为鬼雄", "24 临江仙·夜归临皋"],
    },
    "chinese_handwriting.png": {
        "description": "真实中文地理手写笔记整页，荧光笔强调、流程箭头及底部手绘空气质量表格。",
        "anchors": ["笔记三 天气与气候", "多变的天气", "1. 天气及其影响", "2. 明天的天气怎么样？", "3. 我们需要洁净的空气"],
        "table_columns": ["空气质量级别", "空气污染指数", "空气质量状况"],
        "table_quality_labels": ["优", "良", "轻度污染", "中度污染", "重度污染"],
    },
}
for record in cache_records:
    cached = record["cache_record"]
    items.append({
        "file": "samples/" + record["file"],
        "kind": "real_chinese_handwriting" if "handwriting" in record["file"] else "real_chinese_scanned_document",
        "source_dataset": "opendatalab/OmniDocBench",
        "revision": OMNI_REV,
        "source_url": cached["source_url"],
        "canonical_source_url": cached["source_url"].replace("https://hf-mirror.com/", "https://huggingface.co/"),
        "retrieval": "Reused an existing publicly downloaded full-page cache; source bytes SHA256 matches the cached benchmark source_image_sha256. No new dataset image download during this audit.",
        "cache_source": record["cache_source"],
        "cache_manifest": record["cache_manifest"],
        "cache_sample_id": cached["id"],
        "source_image_path": cached["page_info"]["image_path"],
        "pixel_transformations": "none; copied original full-page bytes, not the benchmark crop",
        "filename_note": "The existing benchmark stores downloaded JPEG bytes under .png names. This audit preserves those bytes/names for traceability; actual_format below is JPEG.",
        "license_or_restrictions": "OmniDocBench copyright statement: research purposes only, not for commercial use. Do not include these images in the product release. Copyright belongs to source owners.",
        "license_source": "https://github.com/opendatalab/OmniDocBench#copyright-statement",
        "expected_content": omni_expected[record["file"]],
        "visual_review": "Full page visually checked; genuine scan/handwriting with original source marks; no OCR prediction overlays.",
    })

for item in items:
    path = ROOT / item["file"]
    item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    item["bytes"] = path.stat().st_size
    with Image.open(path) as img:
        item["width"], item["height"] = img.size
        item["actual_format"] = img.format

manifest = {
    "schema_version": 1,
    "prepared_on": "2026-09-12",
    "scope": "Six public real-source images for browser workflow testing, not OCR accuracy certification. No generated fixtures included.",
    "visual_review_method": "Opened and inspected every selected image. Rejected candidates containing OCR overlays or artificial test content. Expected anchors are spot-check references, not exhaustive ground truth.",
    "sample_directory": str(SAMPLES),
    "samples": items,
    "provenance_directory": "sample-provenance",
    "rejected_candidates": {
        "real_table.jpg": "PaddleOCR test table contains artificial mixed strings; excluded as synthetic test content.",
        "english_receipt.jpeg": "Real WildReceipt photograph, but the downloaded documentation image has colored annotation overlays; excluded from OCR input.",
        "chinese_scene.jpg": "Side-by-side OCR prediction visualization with colored text boxes; excluded.",
        "chinese_scan.jpg": "Boarding-pass OCR prediction visualization, not a raw scan; excluded.",
        "chinese_document.jpg": "Illustrated Wikihow-style notebook image with font-rendered text; not a genuine handwritten source.",
        "english_scene.png": "Genuine WHO information poster from EasyOCR, visually checked, but omitted from the final six to prioritize handwriting and document coverage.",
    },
}
(ROOT / "sample-sources.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")

lines = [
    "# 公开真实图片样本 — 2026-09-12",
    "",
    "最终选用 6 张公开真实来源图片。每张均已人工视觉检查，没有 OCR 检测框或转写叠加；没有用合成 fixtures 冒充真实样本。用途是工作台用户流程实测，不是严谨 OCR 准确率认证。",
    "",
    "samples 目录仅有这 6 张图片，可以整体导入。来源证据及未采用候选放在独立的 sample-provenance 目录。",
    "",
    "| 文件 | 尺寸 / 实际格式 | 内容 | 来源与许可/限制 |",
    "|---|---|---|---|",
]
for item in items:
    expected = item["expected_content"]["description"]
    source = item["source_url"]
    limits = item["license_or_restrictions"]
    lines.append(f"| [{Path(item['file']).name}]({item['file']}) | {item['width']}×{item['height']} / {item['actual_format']} | {expected} | [固定来源]({source})；{limits} |")
lines += [
    "",
    "两张 OmniDocBench 图片复用 E:/OCR-final-build/public-benchmark 的完整原始页面缓存，并核对 benchmark.json 的 source_image_sha256。它们不是区域裁剪；缓存扩展名为 .png，但文件实际为 JPEG，保留字节与原始命名以便追溯。",
    "",
    "PubTabNet 使用限制依据已下载的 PaddleOCR-table-datasets.md；PaddleOCR、EasyOCR 仓库的 Apache-2.0 LICENSE 已保留。仓库许可证不能被自动解释为原论文/书籍所有第三方内容均可商业再分发。OmniDocBench 明确限研究、禁商业用途，本轮素材不应打包进产品发布。",
    "",
    "## SHA-256",
    "",
    "| 文件 | SHA-256 |",
    "|---|---|",
]
lines += [f"| {Path(item['file']).name} | `{item['sha256']}` |" for item in items]
lines += [
    "",
    "## 检查内容",
    "",
    "sample-sources.json 包含来源 revision、URL、缓存路径、图片尺寸、实际格式、SHA-256、预期文字锚点及表格目视参考值。参考值用于抽查，未宣称独立裁决的完整 ground truth。",
    "",
    "已排除：带检测框的车辆铭牌/登机牌、WildReceipt 文档注释图、人工混合字符串表格、插画式笔记。WHO 英文海报为真实公开海报，但为控制样本数未纳入最终六张。",
]
(ROOT / "sample-sources.md").write_text("\n".join(lines) + "\n", "utf-8")
print(json.dumps({"count": len(items), "manifest": str(ROOT / "sample-sources.json"), "markdown": str(ROOT / "sample-sources.md")}, ensure_ascii=False))
