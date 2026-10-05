import os, re, io, json, uuid, time, shutil, subprocess, tempfile, threading
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, send_file, session, redirect
from functools import wraps

try:
    from dotenv import load_dotenv
    # utf-8-sig: يتحمّل ملفات .env المحفوظة من Notepad (BOM)
    load_dotenv(Path(__file__).resolve().parent / ".env", encoding="utf-8-sig", override=True)
except ImportError:
    pass

from openai import (OpenAI, AuthenticationError, RateLimitError, APIConnectionError,
                    BadRequestError, APIStatusError)

BASE = Path(__file__).resolve().parent
HISTORY = BASE / "history"
HISTORY.mkdir(exist_ok=True)
app = Flask(__name__, static_folder="static")
app.secret_key = os.getenv("SECRET_KEY", "change-this-secret-key")
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "1") == "1",
)
MAX_UPLOAD_MB = 500
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

ALLOWED = {".mp3", ".m4a", ".wav", ".webm", ".mp4", ".mpeg", ".mpga", ".ogg", ".flac", ".aac", ".opus", ".mov", ".mkv"}
DIRECT_OK = {".mp3", ".m4a", ".wav", ".webm", ".mp4", ".mpeg", ".mpga", ".ogg", ".flac"}  # يقبلها OpenAI بدون ffmpeg
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")  # للدقة الأعلى: gpt-4o-transcribe
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "gpt-4o-mini")
CHUNK_SECONDS = 600                 # الهدف: جزء كل 10 دقايق (يتحرك لأقرب لحظة صمت)
DIRECT_LIMIT = 24 * 1024 * 1024
MAX_RUNNING_JOBS = 2
MODES = {"organized", "literal", "summary", "study"}
# سعر الدقيقة بالدولار (تقديري، راجع صفحة أسعار OpenAI؛ ممكن تغيّره بـ PRICE_PER_MIN في .env)
PRICE_PER_MIN = float(os.getenv("PRICE_PER_MIN", {"gpt-4o-mini-transcribe": 0.003}.get(TRANSCRIBE_MODEL, 0.006)))
ENHANCE_FILTER = "highpass=f=80,afftdn=nf=-25,dynaudnorm=f=150:g=15"  # يشيل الدوشة ويرفع الصوت الواطي
HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None

jobs, jobs_lock = {}, threading.Lock()


class UserError(Exception):
    """خطأ رسالته مفهومة للمستخدم."""


class Cancelled(Exception):
    pass


# ---------- الحماية ----------

def auth_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if APP_PASSWORD and not session.get("authenticated"):
            return jsonify(error="يلزم تسجيل الدخول أولًا."), 401
        return fn(*args, **kwargs)
    return wrapper

# ---------- أدوات مساعدة ----------
def get_key():
    k = (os.getenv("OPENAI_API_KEY") or "").strip()
    return k if k.startswith("sk-") and k.isascii() else ""


def update_job(job_id, **kw):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kw, updated=time.time())


def is_cancelled(job_id):
    with jobs_lock:
        return bool(jobs.get(job_id, {}).get("cancel"))


def purge_old_jobs():
    """يمسح الأعمال المنتهية القديمة فقط (الشغالة لا تُمسح مهما طالت)."""
    now = time.time()
    with jobs_lock:
        for k, v in list(jobs.items()):
            if (v["status"] != "running" and now - v["updated"] > 3600) or now - v["created"] > 12 * 3600:
                jobs.pop(k, None)


def media_duration(src: Path):
    if not HAS_FFPROBE:
        return None
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(src)], capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return None


def cut_points(src: Path, dur: float):
    """نقاط قطع قريبة من كل 10 دقايق عند أقرب لحظة صمت، عشان ما تتقطعش كلمة في النص."""
    p = subprocess.run(["ffmpeg", "-nostdin", "-i", str(src), "-vn", "-af", "silencedetect=noise=-35dB:d=0.4",
                        "-f", "null", "-"], capture_output=True, text=True)
    mids = [float(e) - float(d) / 2 for e, d in
            re.findall(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", p.stderr)]
    cuts, target = [], CHUNK_SECONDS
    while target < dur - 90:
        near = [m for m in mids if abs(m - target) <= 90 and m > (cuts[-1] if cuts else 0) + 60]
        c = min(near, key=lambda m: abs(m - target)) if near else target
        cuts.append(round(c, 2))
        target = c + CHUNK_SECONDS
    return cuts


def split_audio(src: Path, workdir: Path, enhance=False):
    """يحوّل الملف لصوت مضغوط (mono 16kHz) ويقسّمه لأجزاء قصيرة. بيشتغل كمان مع الفيديو."""
    if not HAS_FFMPEG:
        if src.suffix.lower() not in DIRECT_OK:
            raise UserError("الصيغة دي محتاجة ffmpeg عشان تتحوّل. ثبّته أو حوّل الملف لـ MP3.")
        if src.stat().st_size <= DIRECT_LIMIT:
            return [src]
        raise UserError("الملف أكبر من 24MB، ومحتاج تثبّت ffmpeg عشان الخادم يقدر يقسّمه تلقائيًا. راجع README.")
    dur = media_duration(src)
    seg = ["-segment_time", str(CHUNK_SECONDS)]
    if dur and dur <= CHUNK_SECONDS * 1.15:
        seg = ["-segment_time", "100000"]           # ملف قصير: جزء واحد
    elif dur:
        try:
            cuts = cut_points(src, dur)
            if cuts:
                seg = ["-segment_times", ",".join(map(str, cuts))]
        except Exception:
            pass                                      # لو فشل كشف الصمت نرجع للتقسيم الثابت
    cmd = ["ffmpeg", "-nostdin", "-y", "-i", str(src), "-vn", *(["-af", ENHANCE_FILTER] if enhance else []), "-ac", "1", "-ar", "16000", "-b:a", "48k",
           "-f", "segment", *seg, "-reset_timestamps", "1", str(workdir / "part_%04d.mp3")]
    p = subprocess.run(cmd, capture_output=True)
    parts = sorted(workdir.glob("part_*.mp3"))
    if p.returncode != 0 or not parts:
        raise UserError("مقدرتش أقرأ الصوت من الملف. ممكن يكون تالف أو مفيهوش صوت.")
    return parts


def split_sentences(text: str, size: int):
    """يقسم النص لأجزاء ≤ size حرف عند نهايات الجمل."""
    chunks, cur = [], ""
    for s in re.split(r"(?<=[.!?؟])\s+", text.strip()):
        if cur and len(cur) + len(s) > size:
            chunks.append(cur.strip()); cur = ""
        cur += s + " "
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def to_paragraphs(text: str, per: int = 4) -> str:
    sents = [s.strip() for s in re.split(r"(?<=[.!?؟])\s+", text) if s.strip()]
    if len(sents) <= 1 and len(text) > 500:  # نص من غير ترقيم
        words, sents, cur = text.split(), [], []
        for w in words:
            cur.append(w)
            if sum(len(x) + 1 for x in cur) > 350:
                sents.append(" ".join(cur)); cur = []
        if cur:
            sents.append(" ".join(cur))
        per = 1
    return "\n\n".join(" ".join(sents[i:i + per]) for i in range(0, len(sents), per))


SUMMARY_SYSTEM = (
    "أنت مساعد متخصص في تلخيص المحاضرات. لخّص النص بالعربية الفصحى المبسطة في: "
    "(1) فكرة المحاضرة في جملتين، (2) النقاط الرئيسية مرتبة بعناوين قصيرة، (3) أهم المصطلحات والتعريفات. "
    "لا تضف أي معلومة غير موجودة في النص. اكتب نصًا عاديًا بدون رموز ماركداون مثل ** أو #؛ استخدم الشرطة - للنقاط."
)


def llm(client, system, content):
    r = client.chat.completions.create(
        model=SUMMARY_MODEL, temperature=0.2,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": content}])
    return r.choices[0].message.content.strip()


def summarize(client, text: str) -> str:
    partials = [llm(client, SUMMARY_SYSTEM, p) for p in split_sentences(text, 12000)]
    if len(partials) == 1:
        return partials[0]
    return llm(client, SUMMARY_SYSTEM + " النص التالي عبارة عن ملخصات أجزاء متتالية من نفس المحاضرة؛ ادمجها في ملخص واحد.",
               "\n\n---\n\n".join(partials))


STUDY_SYSTEM = (
    "أنت مساعد مذاكرة لطالب جامعي. من نص المحاضرة اكتب بالعربية، بدون ماركداون، وبالشكل التالي بالضبط: "
    "سطر 'أسئلة المراجعة' ثم 10 أسئلة اختيار من متعدد مرقمة؛ كل سؤال تحته 4 اختيارات في أسطر منفصلة تبدأ بـ أ) ب) ج) د) "
    "ثم سطر 'الإجابة: ' والحرف الصحيح. بعدها سطر 'مصطلحات للحفظ' ثم نقاط تبدأ بـ - بصيغة 'المصطلح: تعريفه'. "
    "الأسئلة والمصطلحات من النص فقط، ولا تضف أي معلومة من خارجه."
)


def fmt_ts(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def save_history(job_id, title, mode, text, warning):
    d = dict(id=job_id, title=title or "محاضرة", created=time.time(), mode=mode, text=text, warning=warning)
    (HISTORY / f"{job_id}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def friendly_error(e: Exception) -> str:
    if isinstance(e, (UserError, Cancelled)):
        return str(e) or "اتلغت العملية."
    if isinstance(e, AuthenticationError):
        return "مفتاح OpenAI API غير صحيح. راجع المفتاح وأعد تشغيل الخادم."
    if isinstance(e, RateLimitError):
        if "quota" in str(e).lower() or getattr(e, "code", "") == "insufficient_quota":
            return "رصيد حساب OpenAI خلص. اشحن الرصيد وجرّب تاني."
        return "ضغط كبير على الطلبات. استنى دقيقة وجرّب تاني."
    if isinstance(e, APIConnectionError):
        return "مشكلة في الاتصال بالإنترنت أو بخدمة OpenAI. جرّب تاني."
    if isinstance(e, BadRequestError):
        return "OpenAI رفض الملف (صيغة غير مدعومة أو ملف تالف أو جزء طويل جدًا). ثبّت ffmpeg أو حوّله لـ MP3."
    if isinstance(e, APIStatusError):
        return f"خدمة التفريغ رجّعت خطأ ({e.status_code}). جرّب تاني بعد شوية."
    return "حصل خطأ غير متوقع أثناء التفريغ. شوف سجل الخادم (Terminal) للتفاصيل."


# ---------- الشغل في الخلفية ----------
def worker(job_id, src: Path, workdir: Path, mode: str, hint: str, key: str, title="", stamps=True, enhance=False):
    texts, total, warning = [], 0, None
    try:
        try:
            client = OpenAI(api_key=key, max_retries=3, timeout=300)
            update_job(job_id, stage="تجهيز الملف وتقسيمه…", progress=5)
            parts = split_audio(src, workdir, enhance)
            total, tail = len(parts), ""
            starts = [0.0]   # بداية كل جزء بالثواني (للتوقيت)
            for pt in parts[:-1]:
                starts.append(starts[-1] + (media_duration(pt) or CHUNK_SECONDS))
            for i, part in enumerate(parts):
                if is_cancelled(job_id):
                    raise Cancelled()
                update_job(job_id, stage=f"تفريغ الجزء {i + 1} من {total}…", progress=int(10 + 75 * i / total))
                kwargs = {}
                prompt = (hint[:400] + " " + tail).strip()
                if prompt:
                    kwargs["prompt"] = prompt   # سياق من الجزء السابق يحسّن الاستمرارية
                with open(part, "rb") as fh:
                    r = client.audio.transcriptions.create(
                        model=TRANSCRIBE_MODEL, language="ar",
                        file=("audio" + part.suffix, fh, "application/octet-stream"), **kwargs)
                texts.append(r.text.strip())
                tail = r.text.strip()[-300:]
        except Exception as e:
            if not isinstance(e, (UserError, Cancelled)):
                app.logger.exception("Job %s failed", job_id)
            msg = friendly_error(e)
            if not any(texts):                       # مفيش حاجة تتنقذ
                update_job(job_id, status="error", error=msg)
                return
            warning = f"{msg} النص اللي تحت بيغطي {len(texts)} من {total} جزء فقط."

        full = " ".join(t for t in texts if t)
        if not full:
            update_job(job_id, status="error", error="ملقيتش كلام واضح في الملف.")
            return
        def body():
            if stamps:
                return "\n\n".join(f"⏱ {fmt_ts(st)}\n\n" + (t if mode == "literal" else to_paragraphs(t))
                                    for t, st in zip(texts, starts) if t)
            return full if mode == "literal" else to_paragraphs(full)

        if mode in {"summary", "study"}:
            update_job(job_id, stage="جاري تلخيص المحاضرة…", progress=90)
            try:
                summ = summarize(client, full)
                out = "الملخص\n\n" + summ
                if mode == "study":
                    update_job(job_id, stage="جاري تجهيز أسئلة المراجعة…", progress=94)
                    try:
                        out += "\n\n" + llm(client, STUDY_SYSTEM, full if len(full) <= 30000 else summ)
                    except Exception as e:
                        app.logger.exception("Study pack failed")
                        warning = ((warning + " ") if warning else "") + "تعذر إنشاء الأسئلة (" + friendly_error(e) + ")."
                out += "\n\n" + "─" * 20 + "\n\nالنص الكامل\n\n" + body()
            except Exception as e:                   # فشل الملخص ما يضيّعش التفريغ
                app.logger.exception("Summary failed")
                out = body()
                warning = ((warning + " ") if warning else "") + "تعذر كتابة الملخص (" + friendly_error(e) + ") وده النص الكامل."
        else:
            out = body()
        try:
            save_history(job_id, title, mode, out, warning)
        except OSError:
            app.logger.exception("History save failed")
        update_job(job_id, status="done", progress=100, stage="تم", text=out, warning=warning)
    except Exception as e:
        app.logger.exception("Job %s crashed", job_id)
        update_job(job_id, status="error", error=friendly_error(e))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------- المسارات ----------
@app.post("/login")
def login():
    if not APP_PASSWORD:
        return jsonify(ok=True)
    password = (request.get_json(silent=True) or {}).get("password", "")
    if password != APP_PASSWORD:
        return jsonify(error="كلمة المرور غير صحيحة."), 401
    session["authenticated"] = True
    return jsonify(ok=True)

@app.post("/logout")
def logout():
    session.clear()
    return jsonify(ok=True)

@app.get("/auth")
def auth_status():
    return jsonify(required=bool(APP_PASSWORD), authenticated=(not APP_PASSWORD or bool(session.get("authenticated"))))

@app.get("/")
def home():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/health")
def health():
    return jsonify(key_set=bool(get_key()), ffmpeg=HAS_FFMPEG,
                   max_mb=MAX_UPLOAD_MB if HAS_FFMPEG else 24, price_per_min=PRICE_PER_MIN,
                   allowed=sorted(ALLOWED if HAS_FFMPEG else DIRECT_OK))


@app.errorhandler(413)
def too_big(_):
    return jsonify(error=f"الملف أكبر من الحد المسموح ({MAX_UPLOAD_MB}MB)."), 413


@app.post("/transcribe")
@auth_required
def transcribe():
    key = get_key()
    if not key:
        return jsonify(error="مفتاح OpenAI API ناقص أو غلط في ملف .env (لازم يبدأ بـ sk-). راجع README."), 503
    f = request.files.get("audio")
    if not f:
        return jsonify(error="اختار ملف صوتي الأول."), 400
    ext = Path(f.filename or "").suffix.lower()
    if ext not in (ALLOWED if HAS_FFMPEG else DIRECT_OK):
        return jsonify(error="صيغة الملف غير مدعومة" + ("" if HAS_FFMPEG else " بدون ffmpeg") + ". جرّب MP3 أو M4A أو WAV أو MP4."), 400
    mode = request.form.get("mode", "organized")
    if mode not in MODES:
        mode = "organized"
    hint = (request.form.get("hint") or "").strip()[:400]
    title = (request.form.get("title") or "").strip()[:100]
    stamps = request.form.get("stamps", "1") == "1"
    enhance = request.form.get("enhance") == "1" and HAS_FFMPEG

    purge_old_jobs()
    job_id = uuid.uuid4().hex
    with jobs_lock:   # الفحص والحجز في خطوة واحدة عشان ما يعديش طلبين مع بعض
        if sum(1 for j in jobs.values() if j["status"] == "running") >= MAX_RUNNING_JOBS:
            return jsonify(error="في عمليات تفريغ شغالة حاليًا. استنى لما تخلص وجرّب تاني."), 429
        now = time.time()
        jobs[job_id] = dict(status="running", progress=0, stage="في الانتظار…", created=now, updated=now)

    workdir = Path(tempfile.mkdtemp(prefix="lecture_"))
    try:
        src = workdir / f"input{ext}"       # مش بنستخدم اسم الملف الأصلي على القرص
        f.save(src)
        if not HAS_FFMPEG and src.stat().st_size > DIRECT_LIMIT:
            raise UserError("الملف أكبر من 24MB، ومحتاج تثبّت ffmpeg عشان يتقسّم تلقائيًا. راجع README.")
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        with jobs_lock:
            jobs.pop(job_id, None)
        return jsonify(error=friendly_error(e) if isinstance(e, UserError) else "فشل حفظ الملف على الخادم."), 413

    threading.Thread(target=worker, args=(job_id, src, workdir, mode, hint, key, title, stamps, enhance), daemon=True).start()
    return jsonify(job_id=job_id), 202


@app.get("/status/<job_id>")
@auth_required
def status(job_id):
    with jobs_lock:
        j = jobs.get(job_id)
        if not j:
            return jsonify(error="العملية مش موجودة (ممكن الخادم اتعمله إعادة تشغيل). ارفع الملف من جديد."), 404
        return jsonify({k: v for k, v in j.items() if k not in {"created", "updated", "cancel"}})


@app.post("/cancel/<job_id>")
@auth_required
def cancel(job_id):
    update_job(job_id, cancel=True)
    return jsonify(ok=True)


# ---------- سجل المحاضرات ----------
HID = re.compile(r"^[0-9a-f]{32}$")


def _hfile(hid):
    return HISTORY / f"{hid}.json" if HID.match(hid) else None


@app.get("/history")
@auth_required
def history_list():
    items = []
    for f in HISTORY.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            items.append(dict(id=d["id"], title=d["title"], created=d["created"], mode=d["mode"],
                              words=len(d["text"].split())))
        except (OSError, ValueError, KeyError):
            continue
    return jsonify(sorted(items, key=lambda x: -x["created"]))


@app.route("/history/<hid>", methods=["GET", "POST", "DELETE"])
@auth_required
def history_item(hid):
    f = _hfile(hid)
    if not f or not f.exists():
        return jsonify(error="المحاضرة مش موجودة."), 404
    if request.method == "DELETE":
        f.unlink(missing_ok=True)
        return jsonify(ok=True)
    d = json.loads(f.read_text(encoding="utf-8"))
    if request.method == "POST":                       # حفظ التعديلات
        d["text"] = (request.get_json(silent=True) or {}).get("text", d["text"])
        f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return jsonify(ok=True)
    return jsonify(d)


# ---------- Word ----------
def _rtl_par(p):
    from docx.oxml import OxmlElement
    bidi = OxmlElement("w:bidi")   # ترتيب العناصر مهم لـ Word، لذلك نضيفه قبل spacing/jc
    p._p.get_or_add_pPr().insert_element_before(
        bidi, "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing",
        "w:mirrorIndents", "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
        "w:outlineLvl", "w:rPr", "w:sectPr")


def _run(p, text, size=13, bold=False):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    r = p.add_run(text)
    r.bold = bold
    r.font.size = __import__("docx").shared.Pt(size)
    rPr = r._r.get_or_add_rPr()
    rf = rPr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts"); rPr.insert(0, rf)
    for a in ("w:ascii", "w:hAnsi", "w:cs"):   # cs = خط الحروف العربية
        rf.set(qn(a), "Arial")
    if bold:
        rPr.append(OxmlElement("w:bCs"))
    szcs = OxmlElement("w:szCs"); szcs.set(qn("w:val"), str(size * 2)); rPr.append(szcs)
    rPr.append(OxmlElement("w:rtl"))


@app.post("/export/docx")
@auth_required
def export_docx():
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Mm, Pt
    text = (request.get_json(silent=True) or {}).get("text", "")
    if not text.strip():
        return jsonify(error="مفيش نص للتصدير."), 400
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)   # A4
    p = doc.add_paragraph(); _rtl_par(p); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(p, "تفريغ المحاضرة", 22, True)
    for line in text.split("\n"):
        s = line.strip()
        if not s or set(s) <= {"─", "-", "—"}:
            continue
        bullet = re.match(r"^[-•*]\s+(.*)", s)
        heading = s in {"الملخص", "النص الكامل", "أسئلة المراجعة", "مصطلحات للحفظ"}
        stamp = s.startswith("⏱ ")
        p = doc.add_paragraph(style="List Bullet" if bullet else None)
        _rtl_par(p)
        p.paragraph_format.space_after = Pt(8)
        _run(p, bullet.group(1) if bullet else s, 17 if heading else 11 if stamp else 13, heading or stamp)
    buf = io.BytesIO(); doc.save(buf); buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="lecture.docx",
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


if __name__ == "__main__":
    # 127.0.0.1 افتراضيًا: عشان محدش على نفس الشبكة يستهلك رصيد API بتاعك.
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5000")), debug=False, threaded=True)
