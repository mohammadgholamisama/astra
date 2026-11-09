import { useState } from "react";
import { BookOpen, ChevronDown, Info } from "lucide-react";

export type GuideItem = {
  title: string;
  body: string;
  tone?: "ok" | "warn" | "danger" | "muted";
};

type Props = {
  title?: string;
  intro?: string;
  items: GuideItem[];
  defaultOpen?: boolean;
};

export default function AnalyticsGuide({
  title = "راهنمای آمار",
  intro,
  items,
  defaultOpen = false,
}: Props) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <section className={`an-guide${open ? " is-open" : ""}`}>
      <button
        type="button"
        className="an-guide-toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="an-guide-toggle-main">
          <BookOpen size={16} />
          <span>
            <strong>{title}</strong>
            <small>معنی هر آیتم و نحوهٔ محاسبه</small>
          </span>
        </span>
        <ChevronDown size={18} className="an-guide-chevron" />
      </button>
      {open ? (
        <div className="an-guide-body">
          {intro ? <p className="an-guide-intro">{intro}</p> : null}
          <div className="an-guide-grid">
            {items.map((it) => (
              <article
                key={it.title}
                className={`an-guide-item${it.tone ? ` tone-${it.tone}` : ""}`}
              >
                <header>
                  <Info size={14} />
                  <h4>{it.title}</h4>
                </header>
                <p>{it.body}</p>
              </article>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}

export const USER_ANALYTICS_GUIDE: GuideItem[] = [
  {
    title: "آنلاین الان",
    body: "کاربرانی که در چند دقیقه اخیر مصرف خام قابل‌توجه داشته‌اند. از دلتای ترافیک حساب می‌شود، نه فقط باز کردن ساب.",
    tone: "ok",
  },
  {
    title: "مصرف خام",
    body: "حجم واقعی روی پنل‌ها بدون ضریب گروه. برای مقایسه واقعی سرعت و رفتار است؛ با «مصرف با ضریب» صورتحساب فرق دارد.",
  },
  {
    title: "وصل و سالم",
    body: "اخیراً مصرف فعال دارد و اتصال پایدار به‌نظر می‌رسد.",
    tone: "ok",
  },
  {
    title: "اذیت می‌شود",
    body: "ساب را اخیراً آپدیت کرده ولی مصرف نزدیک صفر است — معمولاً کانفیگ‌ها کار نمی‌کنند یا به اینترنت وصل نمی‌شود.",
    tone: "danger",
  },
  {
    title: "بلااستفاده / استفاده‌نشده",
    body: "چند روز بدون مصرف و بدون آپدیت ساب. استفاده‌نشده برای اکانت‌های تازه‌ساخته‌ای است که هنوز چیزی رد نکرده‌اند.",
    tone: "muted",
  },
  {
    title: "چنددستگاهی",
    body: "چند کلاینت یا چند شبکهٔ اینترنت مختلف روی یک لینک ساب دیده شده؛ احتمال اشتراک‌گذاری ساب.",
    tone: "warn",
  },
  {
    title: "نشست آنلاین",
    body: "بازهٔ پیوسته‌ای که کاربر مصرف داشته. نشست فعلی با نشست قبلی از نظر حجم و سرعت مقایسه می‌شود.",
  },
  {
    title: "بالاترین سرعت عمری",
    body: "بیشترین نرخ تقریبی (بایت بر ثانیه) که از نمونه‌برداری مصرف خام از روز اول دیده شده.",
  },
  {
    title: "دستگاه / اینترنت ساب",
    body: "از User-Agent و IP درخواست آپدیت ساب: اپ کلاینت (v2rayNG و …) و شبکه‌های /24 متمایز.",
  },
  {
    title: "نمودار ساعات روز",
    body: "جمع مصرف در هر ساعت شبانه‌روز؛ ستون طلایی = بازهٔ اوج مصرف.",
  },
];

export const PANEL_ANALYTICS_GUIDE: GuideItem[] = [
  {
    title: "آنلاین کل / هر پنل",
    body: "کاربرانی که روی همان پنل اخیراً مصرف خام داشته‌اند. یک نفر می‌تواند همزمان روی چند پنل آنلاین باشد.",
    tone: "ok",
  },
  {
    title: "مصرف خام پنل",
    body: "جمع دلتای used_traffic همهٔ اکانت‌های آن پنل — بدون ضریب. ساعت/روز/هفته/ماه از همین باکت‌هاست.",
  },
  {
    title: "نرخ و سرانه",
    body: "نرخ ≈ مصرف ساعت ÷ ۳۶۰۰. سرانه = همان نرخ تقسیم بر تعداد آنلاین همان پنل برای مقایسهٔ بازدهی پنل‌ها.",
  },
  {
    title: "اپراتور آنلاین‌ها",
    body: "تخمین از IP آخرین آپدیت ساب (ایرانسل / همراه / رایتل). دقیق مثل انتخاب دستی اپ پینگ نیست.",
    tone: "warn",
  },
  {
    title: "دستگاه (آیفون/شیائومی/…)",
    body: "از User-Agent کلاینت هنگام آپدیت ساب استخراج می‌شود.",
  },
  {
    title: "تست / top100",
    body: "از همگام‌سازی ping-hub: چند کانفیگ این پنل در دور تست امتیاز گرفته و چندتایش بین ۱۰۰تای برتر بوده.",
  },
  {
    title: "خوب",
    body: "کانفیگ‌هایی با پینگ/پایداری/پکت‌لاس در آستانهٔ معقول در همان دور تست.",
    tone: "ok",
  },
  {
    title: "همه اپراتور",
    body: "اگر دادهٔ per-operator از هاب باشد روی هر سه اپراتور OK؛ وگرنه تقریبی با چند دستگاه تست‌کننده.",
  },
  {
    title: "بازهٔ مصرف روز",
    body: "در چه ساعاتی از شبانه‌روز این پنل بیشتر لود شده؛ برای ظرفیت و زمان پیک مفید است.",
  },
  {
    title: "ستون تست جدول",
    body: "عدد اول = آخرین دور تست، عدد دوم = جمع عمری ثبت‌شده برای آن پنل.",
  },
];
