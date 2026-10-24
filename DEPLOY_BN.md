# MediaNest সেটআপ গাইড (বাংলা)

**তৈরি করেছে: ReelsByTanjim**

## GitHub ও Render

কোডটি `myselftanjim/video-downloader-backend` repository-তে রয়েছে। `main` branch update হলেই Render service auto-deploy করার জন্য configuration দেওয়া আছে।

1. Render Dashboard-এ ঢোকো: https://dashboard.render.com/
2. **New → Web Service** নির্বাচন করো এবং GitHub repository `myselftanjim/video-downloader-backend` connect করো।
3. Runtime হিসেবে Docker এবং plan হিসেবে Free নির্বাচন করো। Region: Singapore।
4. Root directory খালি রাখো; repository root-এ Dockerfile আছে।
5. `APP_PASSWORD` নামে একটি environment variable সেট করো। এটি পরিবারে ভাগ করা দীর্ঘ, আলাদা access code হবে।
6. `SESSION_SECRET` নামে environment variable হিসেবে long random value সেট করো (Blueprint ব্যবহার করলে Render-generated secret ব্যবহার করো)।
7. Optional: `MAX_DOWNLOAD_MB=400` রাখো।
8. Deploy শেষ হলে `https://YOUR-SERVICE.onrender.com/health` খুলে `{"status":"ok","app":"medianest"}` দেখো। Homepage খুলে access code দিয়ে লগইন করো।

## ফিচার ব্যবহার

1. Downloader-এ YouTube/Facebook/Instagram/Pinterest/TikTok-এর public video link পেস্ট করে **Find qualities** চাপো। Pinterest-এর `pin.it` share link-ও দিতে পারো; না খুললে full Pin URL ব্যবহার করো.
2. **Images** tab-এ direct image CDN URL বা supported public post/Pin/gallery link দাও। Pinterest, Instagram, Facebook, TikTok, X/Twitter, Reddit, Tumblr, Flickr, Imgur, DeviantArt, Pixiv, Pexels, Unsplash, Pixabay, ArtStation, Behance, Bluesky, Wikimedia-সহ gallery-dl-supported source চেষ্টা করবে। Direct CDN-এ source/original variant আগে চেষ্টা করা হবে; একাধিক image ZIP হবে; video link হলে best available thumbnail fallback চেষ্টা করবে। কিছু extractor-এ cookies/login দরকার হতে পারে, যা shared server-এ configure করা নেই।
3. **Converter** tab-এ নিজের video upload করে MP4/MP3 export করো.
4. Available video quality বা MP3 নির্বাচন করো.
5. ১০ মিনিটের কম duration হলে Trim option দেখা যাবে না। Duration ১০ মিনিট বা বেশি হলে checkbox অন করে start/end seconds দাও.
6. **Prepare download** চাপো; কাজ শেষ হলে Download বাটন আসবে।

## Free plan-এর সীমা

- Render Free service idle থাকলে sleep করে; প্রথম request-এ startup delay হতে পারে। Guaranteed 24/7 uptime নেই।
- Free CPU/RAM ও 400 MB output cap-এর কারণে বড়, দীর্ঘ বা 4K ভিডিও ব্যর্থ হতে পারে।
- Filesystem ephemeral; ফাইল ডাউনলোডের পর বা ৩০ মিনিটের মধ্যে মুছে যায়।
- একবারে একটি processing job চলবে।
- সব social URL কাজ করবে না; platform rules, source availability ও login/DRM controls respect করতে হবে। এই টুল কোনো সুরক্ষা পাশ কাটায় না।


## Telegram Bot চালু করার ধাপ (Phase A)

Telegram bot চালুর আগে GitHub-এর নতুন version Render-এ deploy হতে হবে। Bot token আমাকে বা অন্য কাউকে chat-এ পাঠাবে না; এটি শুধু Render-এর environment variable-এ রাখবে।

### ধাপ ১ — Bot তৈরি করো
1. Telegram-এ \`@BotFather\` খোলো।
2. \`/newbot\` পাঠাও।
3. Bot-এর display name ও username দাও (username-এর শেষে \`bot\` থাকতে হবে)।
4. BotFather যে token দেবে, সেটি কপি করে নিরাপদে রাখো। Token public করো না।

### ধাপ ২ — নিজের Telegram user ID বের করো
1. Telegram-এ \`@userinfobot\` খুলে \`/start\` দাও।
2. তোমার numeric user ID কপি করো। Bot-টি শুধু এই ID allowlist-এ থাকলে ব্যবহার করা যাবে।
3. একাধিক trusted user অনুমতি দিতে চাইলে ID-গুলো comma দিয়ে আলাদা করে লিখতে হবে। এখন নিজের ID দিয়েই শুরু করো।

### ধাপ ৩ — Webhook secret তৈরি করো
একটি লম্বা random secret তৈরি করো যাতে শুধু English letters, numbers, underscore (\`_\`) ও hyphen (\`-\`) থাকে। উদাহরণ হিসেবে token generator-এর output ব্যবহার করতে পারো; এই secret-ও public করো না।

### ধাপ ৪ — Render environment variables যোগ করো
1. [Render Dashboard](https://dashboard.render.com/) → MediaNest web service → **Environment** খোলো।
2. নিচের environment variables যোগ করো:
   - \`TELEGRAM_BOT_TOKEN\` = BotFather-এর token
   - \`TELEGRAM_ALLOWED_USER_IDS\` = তোমার numeric Telegram user ID
   - \`TELEGRAM_WEBHOOK_SECRET\` = ধাপ ৩-এর random secret
   - \`TELEGRAM_WEBHOOK_URL\` = \`https://medianest.reelsbytanjim.com/telegram/webhook\` (custom domain চালু থাকলে); না হলে Render service-এর HTTPS URL-এর শেষে \`/telegram/webhook\` যোগ করো।
3. **Save, Build & Deploy** করো। এই চারটি variable পূরণ না থাকলে bot ইচ্ছাকৃতভাবে disabled থাকবে।

### ধাপ ৫ — পরীক্ষা করো
1. Deploy সফল হলে Telegram-এ নিজের bot-এর chat খোলো এবং \`/start\` দাও।
2. **Video Download** বেছে একটি public video link পাঠাও। Bot-এর দেওয়া quality/format বেছে নাও।
3. **Image Download** বেছে একটি public post/image link পরীক্ষা করো।
4. **My Jobs / Status** ও **Cancel Job**-ও পরীক্ষা করো।
5. 47 MiB-এর চেয়ে বড় output Telegram-এ ফাইল হিসেবে পাঠানো যাবে না; bot ১৫ মিনিট মেয়াদি temporary link দেওয়ার চেষ্টা করবে। Render restart হলে চলমান job বা in-memory link হারিয়ে যেতে পারে।

### কী কী এখনো বাকি
কোড GitHub-এ push করা থাকলেও live deployment ও Telegram end-to-end test না হওয়া পর্যন্ত bot-কে fully working ধরা যাবে না। Bot private chat এবং allowlisted user ID-তেই কাজ করে; private/login-required content বা platform access control bypass করে না।


## Phase B — Website ও Telegram-এর Shared Job Management

- Website-এ login করার পর **Jobs** tab খুললে website এবং Telegram থেকে শুরু করা সাম্প্রতিক job একসঙ্গে দেখতে পারবে।
- চলমান job-এর progress ও status দেখা যায়; website থেকে Cancel করা যায়, আর completed job-এর temporary file পাওয়া গেলে Download করা যায়।
- Telegram-এ **My Jobs / Status** থেকে website-এর সাম্প্রতিক job-ও দেখতে পারবে। Allowlisted bot user running website job cancel করতে এবং completed result Telegram-এ পাঠাতে পারবে।
- Multi-image source থেকে একাধিক ছবি পাওয়া গেলে backend ZIP তৈরি করে। Bot progress update পাঠায়; Telegram-এ 47 MiB পর্যন্ত ফাইল সরাসরি পাঠানোর চেষ্টা করে, বড় হলে ১৫ মিনিট মেয়াদি secure download link দেয়।
- Job history এখনো RAM-এ থাকে, প্রায় ৩০ মিনিটে মুছে যায় এবং Render restart হলে হারিয়ে যায়। Permanent job history বা persistent cloud file storage Phase B-র অংশ নয়; সেটা পরের পর্যায়ের কাজ।



## Phase C — Telegram থেকে Video Convert

1. Telegram Bot-এর menu থেকে **Convert a video** চাপো, অথবা \`/convert\` পাঠাও।
2. MP4, MOV, MKV, WebM, AVI, M4V, MPEG, MPG অথবা 3GP video file পাঠাও।
3. Bot video inspect করে duration, audio আছে কি না এবং resolution দেখে MP4 quality ও MP3 option দেখাবে।
4. ১০ মিনিট বা তার বেশি duration হলে **Trim then convert** চাপতে পারবে। এরপর start,end seconds পাঠাও; যেমন \`15,45\`।
5. Output format ও quality বেছে নাও। Bot progress দেখাবে এবং কাজ শেষে file পাঠানোর চেষ্টা করবে; বড় output হলে temporary link দিতে পারে।

**Telegram file limit:** সাধারণ hosted Bot API দিয়ে bot সর্বোচ্চ ২০ MB পর্যন্ত incoming file download করতে পারে। এর বেশি হলে website-এর Converter ব্যবহার করো; website upload limit বর্তমানে ৯০ MB। Telegram file limits: https://core.telegram.org/bots/faq

## Phase C — Error reporting

Jobs tab এবং Telegram-এর job status-এ conversion বা download ব্যর্থ হলে error code ও পরবর্তী করণীয় দেখাবে। Error code সাধারণ কারণ নির্দেশ করে; platform-এর login/access restriction bypass করা হয় না। Automated tests Python syntax/import, endpoints, shared history, trim parsing, conversion buttons ও website JavaScript পরীক্ষা করে।


## Security

- App publicly reachable, কিন্তু access code ছাড়া API ব্যবহার করা যাবে না।
- Environment secrets কখনো GitHub-এ commit করবে না।
- পরিবারে শেয়ার করা code-টি নিরাপদে রাখবে। দরকার হলে Render dashboard থেকে APP_PASSWORD বদলাও।
