# ClipNest সেটআপ গাইড (বাংলা)

## GitHub ও Render

কোডটি `myselftanjim/video-downloader-backend` repository-তে রয়েছে। `main` branch update হলেই Render service auto-deploy করার জন্য configuration দেওয়া আছে।

1. Render Dashboard-এ ঢোকো: https://dashboard.render.com/
2. **New → Web Service** নির্বাচন করো এবং GitHub repository `myselftanjim/video-downloader-backend` connect করো।
3. Runtime হিসেবে Docker এবং plan হিসেবে Free নির্বাচন করো। Region: Singapore।
4. Root directory খালি রাখো; repository root-এ Dockerfile আছে।
5. `APP_PASSWORD` নামে একটি environment variable সেট করো। এটি পরিবারে ভাগ করা দীর্ঘ, আলাদা access code হবে।
6. `SESSION_SECRET` নামে environment variable হিসেবে long random value সেট করো (Blueprint ব্যবহার করলে Render-generated secret ব্যবহার করো)।
7. Optional: `MAX_DOWNLOAD_MB=400` রাখো।
8. Deploy শেষ হলে `https://YOUR-SERVICE.onrender.com/health` খুলে `{"status":"ok","app":"clipnest"}` দেখো। Homepage খুলে access code দিয়ে লগইন করো।

## ফিচার ব্যবহার

1. YouTube/Facebook/Instagram/TikTok-এর public link পেস্ট করে **Find qualities** চাপো।
2. Available video quality বা MP3 নির্বাচন করো।
3. ১০ মিনিটের কম duration হলে Trim option দেখা যাবে না। Duration ১০ মিনিট বা বেশি হলে checkbox অন করে start/end seconds দাও।
4. **Prepare download** চাপো; কাজ শেষ হলে Download বাটন আসবে।

## Free plan-এর সীমা

- Render Free service idle থাকলে sleep করে; প্রথম request-এ startup delay হতে পারে। Guaranteed 24/7 uptime নেই।
- Free CPU/RAM ও 400 MB output cap-এর কারণে বড়, দীর্ঘ বা 4K ভিডিও ব্যর্থ হতে পারে।
- Filesystem ephemeral; ফাইল ডাউনলোডের পর বা ৩০ মিনিটের মধ্যে মুছে যায়।
- একবারে একটি processing job চলবে।
- সব social URL কাজ করবে না; platform rules, source availability ও login/DRM controls respect করতে হবে। এই টুল কোনো সুরক্ষা পাশ কাটায় না।

## Security

- App publicly reachable, কিন্তু access code ছাড়া API ব্যবহার করা যাবে না।
- Environment secrets কখনো GitHub-এ commit করবে না।
- পরিবারে শেয়ার করা code-টি নিরাপদে রাখবে। দরকার হলে Render dashboard থেকে APP_PASSWORD বদলাও।
