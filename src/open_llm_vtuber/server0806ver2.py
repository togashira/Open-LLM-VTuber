import os
import shutil

from fastapi import FastAPI, Request, HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
import ipaddress

from .routes import init_client_ws_route, init_webtool_routes
from .service_context import ServiceContext
from .config_manager.utils import Config


class SecurityMiddleware(BaseHTTPMiddleware):
    """IP制限とセキュリティスキャン対策のミドルウェア"""
    
    def __init__(self, app, allowed_ips=None, blocked_ips=None):
        super().__init__(app)
        self.allowed_ips = allowed_ips or []
        self.blocked_ips = blocked_ips or ["10.0.0.57"]  # 攻撃元IPをブロック
        
        # 悪意のあるパスパターン
        self.malicious_patterns = [
            '.php', 'wp-admin', 'wp-content', 'wp-includes', 
            'admin.php', 'shell.php', 'filemanager.php',
            '.well-known', 'xmlrpc.php'
        ]
    
    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host
        
        # ブロックリストのIPをチェック
        if client_ip in self.blocked_ips:
            return Response("Access Denied", status_code=403)
        
        # 悪意のあるパスパターンをチェック
        path = request.url.path
        if any(pattern in path.lower() for pattern in self.malicious_patterns):
            print(f"🚨 Malicious request blocked: {client_ip} -> {path}")
            return Response("Not Found", status_code=404)
        
        response = await call_next(request)
        return response


class CustomStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.endswith(".js"):
            response.headers["Content-Type"] = "application/javascript"
        return response


class AvatarStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        allowed_extensions = (".jpg", ".jpeg", ".png", ".gif", ".svg")
        if not any(path.lower().endswith(ext) for ext in allowed_extensions):
            return Response("Forbidden file type", status_code=403)
        return await super().get_response(path, scope)


class WebSocketServer:
    def __init__(self, config: Config):
        self.app = FastAPI()

        # セキュリティミドルウェアを最初に追加
        self.app.add_middleware(SecurityMiddleware)

        # Add CORS
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Load configurations and initialize the default context cache
        default_context_cache = ServiceContext()
        default_context_cache.load_from_config(config)

        # Include WebSocket routes FIRST before any static file mounts
        self.app.include_router(
            init_client_ws_route(default_context_cache=default_context_cache),
        )
        self.app.include_router(
            init_webtool_routes(default_context_cache=default_context_cache),
        )


        # Mount cache directory first (to ensure audio file access)
        if not os.path.exists("cache"):
            os.makedirs("cache")
        self.app.mount(
            "/cache",
            StaticFiles(directory="cache"),
            name="cache",
        )

        # Mount static files（live2d-modelsはconfigからパス取得）
        frontend_config = getattr(config, "frontend", None)
        live2d_model_path = "live2d-models"
        # dict型の場合
        if isinstance(frontend_config, dict):
            live2d_model_path = frontend_config.get("live2d_model_path", live2d_model_path)
        # オブジェクト型の場合
        elif hasattr(frontend_config, "live2d_model_path"):
            live2d_model_path = frontend_config.live2d_model_path
        # 相対パスの場合は絶対パスに変換
        if not os.path.isabs(live2d_model_path):
            live2d_model_path = os.path.abspath(live2d_model_path)
        
        print(f"Live2D models directory: {live2d_model_path}")
        print(f"Directory exists: {os.path.exists(live2d_model_path)}")
        
        # カスタムエンドポイントの定義前にログ出力
        print("🚀 About to define custom Live2D endpoint...")
        
        # StaticFilesの代わりにカスタムエンドポイントを使用
        @self.app.get("/live2d-models/{file_path:path}")
        async def serve_live2d_file(file_path: str):
            """Live2Dファイルを提供するエンドポイント"""
            import mimetypes
            from fastapi.responses import FileResponse
            
            full_path = os.path.join(live2d_model_path, file_path)
            print(f"🎯 Serving Live2D file: {full_path}")
            
            if os.path.exists(full_path) and os.path.isfile(full_path):
                # MIMEタイプを自動検出
                mime_type, _ = mimetypes.guess_type(full_path)
                if mime_type is None:
                    if file_path.endswith('.json'):
                        mime_type = 'application/json'
                    elif file_path.endswith('.moc3'):
                        mime_type = 'application/octet-stream'
                    else:
                        mime_type = 'application/octet-stream'
                
                return FileResponse(
                    path=full_path,
                    media_type=mime_type,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
            else:
                from fastapi import HTTPException
                raise HTTPException(status_code=404, detail="File not found")
        
        print(f"✅ Custom endpoint for /live2d-models -> {live2d_model_path}")
        print("🎯 Custom endpoint defined successfully!")

        self.app.mount(
            "/bg",
            StaticFiles(directory="backgrounds"),
            name="backgrounds",
        )
        self.app.mount(
            "/avatars",
            AvatarStaticFiles(directory="avatars"),
            name="avatars",
        )

        # Mount web tool directory separately from frontend
        self.app.mount(
            "/web-tool",
            CustomStaticFiles(directory="web_tool", html=True),
            name="web_tool",
        )

        # Mount main frontend with specific paths to avoid conflicts with WebSocket routes
        # Use a more specific path instead of root to avoid catching WebSocket routes
        @self.app.get("/")
        async def serve_frontend_root():
            """Serve the main frontend page"""
            from fastapi.responses import FileResponse
            return FileResponse("frontend/index.html")
        
        self.app.mount(
            "/assets",
            CustomStaticFiles(directory="frontend/assets"),
            name="frontend_assets",
        )
        
        # Mount libs directory for Live2D libraries
        self.app.mount(
            "/libs",
            CustomStaticFiles(directory="frontend/libs"),
            name="frontend_libs",
        )
        
        # Mount other frontend static files under /static
        self.app.mount(
            "/static",
            CustomStaticFiles(directory="frontend", html=True),
            name="frontend_static",
        )
        print("✅ Frontend mount re-enabled")

    def run(self):
        pass

    @staticmethod
    def clean_cache():
        """Clean the cache directory by removing and recreating it."""
        cache_dir = "cache"
        if os.path.exists(cache_dir):
            shutil.rmtree(cache_dir)
            os.makedirs(cache_dir)

