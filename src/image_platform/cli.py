import argparse
import json

from .settings import Settings


def main():
    parser = argparse.ArgumentParser(description="图像分类平台")
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("serve", help="运行本机 API 与编译后的 Web 界面")
    server.add_argument("--port", type=int, default=8765)
    worker = sub.add_parser("worker", help="执行后台任务")
    worker.add_argument("--once", action="store_true")
    demo = sub.add_parser("demo", help="生成明确标记的合成图形项目，用于功能验证")
    demo.add_argument("--per-class", type=int, default=20)
    sub.add_parser("doctor", help="检查真实 CUDA 与 ORT 执行环境")
    predict = sub.add_parser("predict", help="使用模型包直接分类，不导入 PyTorch")
    predict.add_argument("package")
    predict.add_argument("images", nargs="+")
    predict.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    predict.add_argument("--auto-accept", action="store_true")
    predict.add_argument("--output")
    args = parser.parse_args()
    settings = Settings.from_env()
    settings.prepare()
    if args.command == "serve":
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)
    elif args.command == "worker":
        from .worker import serve_worker
        serve_worker(settings, once=args.once)
    elif args.command == "demo":
        from .demo import create_demo
        print(json.dumps(create_demo(settings, args.per_class), ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        import onnxruntime as ort
        import torch
        if hasattr(ort, "preload_dlls"):
            ort.preload_dlls()
        result = {"torch": torch.__version__, "cuda_available": torch.cuda.is_available(),
                  "compiled_cuda": torch.version.cuda, "ort": ort.__version__, "providers": ort.get_available_providers()}
        if torch.cuda.is_available():
            result.update(gpu=torch.cuda.get_device_name(0), tensor_check=float(torch.ones(4, device="cuda").sum().item()))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "predict":
        from .predict import predict_images
        from pathlib import Path
        result = predict_images(args.package, args.images, args.device, args.auto_accept)
        rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output:
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        print(rendered)


if __name__ == "__main__":
    main()
