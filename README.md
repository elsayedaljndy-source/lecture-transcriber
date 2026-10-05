# مُفرِّغ المحاضرات — نسخة الموبايل

نسخة جاهزة للنشر على الإنترنت وتشغيلها من Android/iPhone كتطبيق PWA قابل للتثبيت.

## أهم ما تم تحسينه
- FFmpeg داخل Docker: يدعم الفيديو والتقسيم التلقائي للمحاضرات الطويلة.
- حد رفع حتى 500MB من جهة التطبيق، مع تقسيم الصوت إلى أجزاء حوالي 10 دقائق.
- مفتاح OpenAI لا يظهر في الواجهة ولا داخل GitHub.
- حماية اختيارية بكلمة مرور للتطبيق عند النشر.
- تنظيف الملفات المؤقتة بعد انتهاء العملية.
- PWA: يمكن تثبيت التطبيق من Chrome على Android ويظهر كتطبيق مستقل.
- واجهة متجاوبة للموبايل.
- نفس أوضاع: نص منظم، شبه حرفي، ملخص، ومذاكرة.
- تصدير Word وTXT وحفظ سجل المحاضرات.

## النشر باستخدام GitHub + Render

> GitHub هنا لتخزين الكود. تشغيل Flask وFFmpeg لا يتم على GitHub Pages.

1. أنشئ Repository جديد على GitHub.
2. ارفع محتويات هذا المجلد إلى الـRepository.
3. افتح Render وأنشئ Web Service من مستودع GitHub.
4. Render سيستخدم `Dockerfile` تلقائيًا.
5. في Environment Variables ضع:
   - `OPENAI_API_KEY` = مفتاح OpenAI الخاص بك.
   - `APP_PASSWORD` = كلمة مرور قوية للتطبيق.
   - `SECRET_KEY` = قيمة عشوائية طويلة (Render يمكنه توليدها).
6. بعد النشر افتح رابط Render من الموبايل.
7. من Chrome: القائمة ⋮ → Add to Home screen / تثبيت التطبيق.

## ملاحظة مهمة
لا تضع ملف `.env` أو مفتاح OpenAI داخل GitHub. استخدم Environment Variables في Render.

## تشغيل محلي
```bash
pip install -r requirements.txt
python app.py
```

ولأن نسخة النشر تستخدم FFmpeg، يفضل تشغيلها عبر Docker:
```bash
docker build -t lecture-transcriber .
docker run --env-file .env -p 10000:10000 lecture-transcriber
```

## التكلفة
التفريغ يستخدم OpenAI API، وبالتالي تكلفة الاستخدام تعتمد على موديل التفريغ ومدة التسجيل. قيمة `PRICE_PER_MIN` مجرد تقدير لعرضه للمستخدم وليست فاتورة فعلية.

## بناء APK من GitHub

هذا المشروع يحتوي على إعداد Capacitor وGitHub Actions لبناء APK تلقائيًا.

1. ارفع `package.json` و`capacitor.config.ts` ومجلد `.github/workflows` إلى المستودع.
2. بعد نشر السيرفر على Render، من GitHub افتح:
   Settings → Secrets and variables → Actions → Variables → New repository variable
3. أنشئ متغيرًا باسم `BACKEND_URL` وضع فيه رابط Render الكامل مثل `https://your-app.onrender.com`.
4. افتح تبويب Actions ثم `Build Android APK` ثم `Run workflow`.
5. بعد انتهاء البناء، افتح الـworkflow وانزل إلى Artifacts وحمّل `lecture-transcriber-apk`.
6. فك الضغط عن الـartifact وستجد `app-debug.apk`، ثم ثبّته على أندرويد.

مهم: لا تضع `OPENAI_API_KEY` داخل GitHub أو داخل التطبيق. المفتاح يظل في Environment Variables على السيرفر (Render).
