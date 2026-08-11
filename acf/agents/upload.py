"""Agent 10/20 — Upload (YouTube). Unggah klip ke YouTube via Data API v3.

PENGAMAN (penting):
  - Default MATI (upload.enabled: false). Anda harus menyalakannya secara sadar.
  - Default privasi "private". Tidak ada yang tayang publik tanpa keputusan Anda.
  - Hanya mengunggah klip dengan QC & Compliance = PASS (bisa diubah di config).

Butuh setup satu kali di Google Cloud (lihat README bagian Upload):
  - buat project, aktifkan "YouTube Data API v3"
  - buat OAuth Client (Desktop) -> unduh sebagai client_secret.json ke root project
  - login pertama membuka browser -> token.json dibuat otomatis untuk sesi berikutnya

Kuota: sekali upload ~1600 unit dari kuota harian default 10.000 -> ~6 upload/hari.
"""
from __future__ import annotations
import os

from .base import BaseAgent
from .. import control

_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class AuthCancelled(RuntimeError):
    """Login OAuth dibatalkan pengguna atau kehabisan waktu."""


def _run_flow_interruptible(flow, timeout: float | None, cancel_event,
                            bind_host: str = "localhost", port: int = 0,
                            auth_url_cb=None):
    """Seperti flow.run_local_server(port=0), tapi bisa dibatalkan/timeout.

    run_local_server memblokir selamanya bila pengguna menutup tab login;
    di sini server lokal di-poll per detik sambil mengecek cancel_event/deadline.

    bind_host/port: untuk Docker, bind 0.0.0.0 di port tetap yang di-publish;
    redirect_uri tetap http://localhost:<port>/ (syarat OAuth Desktop Google).
    auth_url_cb: dipanggil dengan URL login agar UI bisa menampilkannya —
    di dalam container webbrowser.open() tidak membuka apa pun.
    """
    import time
    import webbrowser
    import wsgiref.simple_server
    import wsgiref.util

    class _Handler(wsgiref.simple_server.WSGIRequestHandler):
        def log_message(self, *args):  # jangan spam stdout
            pass

    class _App:
        last_request_uri = None

        def __call__(self, environ, start_response):
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
            self.last_request_uri = wsgiref.util.request_uri(environ)
            return ["<html><body>Login selesai — tab ini boleh ditutup. "
                    "/ Login complete — you may close this tab.</body></html>"
                    .encode("utf-8")]

    app = _App()
    server = wsgiref.simple_server.make_server(
        bind_host, port, app, handler_class=_Handler)
    try:
        flow.redirect_uri = f"http://localhost:{server.server_port}/"
        auth_url, _ = flow.authorization_url()
        if auth_url_cb is not None:
            try:
                auth_url_cb(auth_url)
            except Exception:  # noqa: BLE001
                pass
        webbrowser.open(auth_url, new=1, autoraise=True)
        server.timeout = 1  # handle_request() bangun tiap detik untuk cek cancel
        deadline = (time.monotonic() + timeout) if timeout else None
        while app.last_request_uri is None:
            if cancel_event is not None and cancel_event.is_set():
                raise AuthCancelled("Login canceled.")
            if deadline is not None and time.monotonic() > deadline:
                raise AuthCancelled("Login timed out — no response from browser.")
            server.handle_request()
        # oauthlib menuntut https pada authorization_response (sama seperti
        # run_local_server): localhost tidak benar-benar TLS, hanya formalitas.
        flow.fetch_token(
            authorization_response=app.last_request_uri.replace("http", "https", 1))
    finally:
        server.server_close()
    return flow.credentials


def authenticate(ucfg: dict, timeout: float | None = None, cancel_event=None,
                 auth_url_cb=None):
    """Bangun service YouTube; jalankan OAuth (membuka browser) bila token belum ada.
    Dipakai oleh UploadAgent maupun dashboard. timeout/cancel_event opsional:
    tanpa keduanya perilaku sama dengan run_local_server standar.

    Env (untuk Docker):
      OAUTH_CALLBACK_PORT  port tetap untuk redirect OAuth (publish port ini!)
      OAUTH_CALLBACK_BIND  host bind server callback (default localhost;
                           di container pakai 0.0.0.0)
    """
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    token_path = ucfg.get("token", "token.json")
    secret_path = ucfg.get("client_secret", "client_secret.json")
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, _SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(secret_path):
                raise RuntimeError(
                    f"'{secret_path}' tidak ada. Unduh OAuth Client (Desktop) dari "
                    f"Google Cloud dan simpan sebagai {secret_path} di root project.")
            flow = InstalledAppFlow.from_client_secrets_file(secret_path, _SCOPES)
            if timeout is None and cancel_event is None and auth_url_cb is None:
                creds = flow.run_local_server(port=0)
            else:
                creds = _run_flow_interruptible(
                    flow, timeout, cancel_event,
                    bind_host=os.environ.get("OAUTH_CALLBACK_BIND", "localhost"),
                    port=int(os.environ.get("OAUTH_CALLBACK_PORT", "0")),
                    auth_url_cb=auth_url_cb)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def credentials_status(ucfg: dict) -> str:
    """Status koneksi: connected | expired | disconnected | no_secret | libs_missing."""
    try:
        from google.oauth2.credentials import Credentials
    except ImportError:
        return "libs_missing"
    token_path = ucfg.get("token", "token.json")
    secret_path = ucfg.get("client_secret", "client_secret.json")
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, _SCOPES)
            if creds and (creds.valid or (creds.expired and creds.refresh_token)):
                return "connected"
            return "expired"
        except Exception:  # noqa: BLE001
            return "expired"
    return "disconnected" if os.path.exists(secret_path) else "no_secret"


class UploadAgent(BaseAgent):
    name = "Upload"
    progress_cb = None  # dashboard boleh mengisi ini untuk melaporkan progres

    def run(self, project, ctx: dict) -> None:
        ucfg = self.cfg.get("upload", {})
        if not ucfg.get("enabled", False):
            self.log.info("Upload dimatikan (upload.enabled=false). Klip siap, tidak diunggah.")
            return

        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError:
            raise RuntimeError(
                "Library upload belum terpasang. Jalankan: pip install -r requirements-upload.txt")

        youtube = self._auth(ucfg)
        privacy = ucfg.get("privacy", "private")
        only_pass = ucfg.get("only_status_pass", True)

        # pilihan klip tertentu (dari dashboard). None = semua.
        only_idxs = ctx.get("only_idxs")
        explicit = only_idxs is not None
        if explicit:
            only_idxs = {int(x) for x in only_idxs}

        todo = [c for c in ctx["clips"]
                if not explicit or c["idx"] in only_idxs]
        self._emit(phase="init", total=len(todo))
        self.log.info("Upload: %d klip akan diproses (privasi=%s).", len(todo), privacy)

        uploaded = []
        ctx["uploaded"] = uploaded  # rujukan hidup: hasil parsial tetap tercatat saat stop
        for clip in todo:
            control.checkpoint()  # stop/pause antar klip
            i = clip["idx"]
            ok, why = self._passes(clip, only_pass, explicit)
            if not ok:
                self.log.info("Klip %02d: dilewati (%s).", i, why)
                self._emit(event="clip_skip", idx=i, reason=why)
                continue
            render = clip.get("render_path")
            if not render or not os.path.isfile(render):
                self.log.warning("Klip %02d: file render tidak ada, dilewati.", i)
                self._emit(event="clip_skip", idx=i, reason="file render hilang")
                continue

            seo = clip.get("seo") or {}
            title = seo.get("title") or f"Clip {i}"
            desc = self._build_description(seo)
            tags = (seo.get("hashtags") or []) + (seo.get("keywords") or [])
            body = {
                "snippet": {"title": title[:100], "description": desc[:4900],
                            "tags": tags[:15], "categoryId": str(ucfg.get("category_id", "22"))},
                "status": {"privacyStatus": privacy,
                           "selfDeclaredMadeForKids": bool(ucfg.get("made_for_kids", False))},
            }
            self.log.info("Klip %02d: upload '%s' ...", i, title)
            self._emit(event="clip_start", idx=i, title=title)
            media = MediaFileUpload(render, chunksize=1024 * 1024 * 4,
                                    resumable=True, mimetype="video/mp4")
            req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            resp = self._resumable(req, i)
            vid = resp.get("id")
            url = f"https://youtu.be/{vid}" if vid else None
            clip["youtube_url"] = url
            uploaded.append({"idx": i, "video_id": vid, "url": url})
            self.log.info("Klip %02d: OK -> %s", i, url)
            self._emit(event="clip_done", idx=i, url=url)

            if ucfg.get("set_thumbnail", False) and clip.get("thumbnail_path"):
                self._try_thumbnail(youtube, vid, clip["thumbnail_path"], i)

        ctx["uploaded"] = uploaded
        self._emit(phase="finish", uploaded=len(uploaded))
        self.log.info("Upload selesai: %d klip terunggah.", len(uploaded))

    # ---- helpers ----
    def _auth(self, ucfg: dict):
        return authenticate(ucfg)

    def _emit(self, **kw):
        cb = getattr(self, "progress_cb", None)
        if cb:
            try:
                cb(kw)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _passes(clip: dict, only_pass: bool, explicit: bool):
        """(boleh_upload, alasan). FAIL selalu diblok. REVIEW boleh bila dipilih manual."""
        qc = clip.get("qc_status", "PASS")
        comp = clip.get("compliance_status", "PASS")
        if qc == "FAIL":
            return False, "QC FAIL"
        if comp == "FAIL":
            return False, "Compliance FAIL"
        if explicit:
            return True, ""  # dipilih manual -> hormati (kecuali FAIL di atas)
        if only_pass and not (qc == "PASS" and comp == "PASS"):
            return False, "bukan PASS"
        return True, ""

    @staticmethod
    def _build_description(seo: dict) -> str:
        desc = seo.get("description", "")
        tags = seo.get("hashtags") or []
        hashline = " ".join("#" + t for t in tags)
        return (desc + "\n\n" + hashline + "\n\n#Shorts").strip()

    def _resumable(self, req, idx: int):
        resp = None
        while resp is None:
            control.checkpoint()  # stop/pause antar chunk upload
            status, resp = req.next_chunk()
            if status:
                p = int(status.progress() * 100)
                self.log.info("   ... %d%%", p)
                self._emit(event="progress", idx=idx, percent=p)
        return resp

    def _try_thumbnail(self, youtube, vid: str, path: str, i: int) -> None:
        from googleapiclient.http import MediaFileUpload
        try:
            youtube.thumbnails().set(videoId=vid, media_body=MediaFileUpload(path)).execute()
            self.log.info("Klip %02d: thumbnail di-set.", i)
        except Exception as e:  # noqa: BLE001
            self.log.warning("Klip %02d: set thumbnail gagal (%s). Abaikan.", i,
                             str(e).split("\n")[0][:120])
