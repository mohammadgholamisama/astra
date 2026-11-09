import { BookOpen, Info, X } from "lucide-react";

export type GuideItem = {
  title: string;
  body: string;
  tone?: "ok" | "warn" | "danger" | "muted";
};

type Props = {
  open: boolean;
  onClose: () => void;
  title?: string;
  intro?: string;
  items: GuideItem[];
};

/** Modal-only guide — open via a separate button on the page. */
export default function AnalyticsGuideModal({
  open,
  onClose,
  title = "راهنمای آمار",
  intro,
  items,
}: Props) {
  if (!open) return null;
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal modal-lg an-guide-modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <header className="modal-head">
          <div>
            <h3>
              <BookOpen size={18} style={{ marginInlineEnd: 8, verticalAlign: -3 }} />
              {title}
            </h3>
            {intro ? <p className="modal-sub">{intro}</p> : null}
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="بستن">
            <X size={18} />
          </button>
        </header>
        <div className="an-guide-modal-body">
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
      </div>
    </div>
  );
}

export const USER_ANALYTICS_GUIDE: GuideItem[] = [
  {
    title: "آنلاین",
    body: "اگر در چند دقیقه اخیر مصرف خام قابل‌توجه داشته باشد آنلاین است (از دلتای ترافیک، نه فقط باز کردن ساب).",
    tone: "ok",
  },
  {
    title: "مصرف خام",
    body: "حجم واقعی روی پنل‌ها بدون ضریب گروه. برای مقایسه واقعی رفتار است.",
  },
  {
    title: "مقایسه ساعت/روز/هفته/ماه",
    body: "مصرف خام بازهٔ جاری در برابر بازهٔ قبلی و درصد تغییر.",
  },
  {
    title: "نمودار ساعتی",
    body: "مصرف هر ساعت در ۴۸ ساعت اخیر. روی هر ستون هاور کنید تا زمان و حجم دیده شود.",
  },
  {
    title: "ساعات روز",
    body: "در کدام ساعت‌های شبانه‌روز بیشتر مصرف داشته. ستون طلایی = اوج.",
  },
  {
    title: "دستگاه / اینترنت",
    body: "از درخواست آپدیت لینک ساب: اپ کلاینت و شبکه‌های اینترنت متمایز (/24).",
  },
  {
    title: "بالاترین سرعت",
    body: "بیشترین نرخ تقریبی مصرف که از نمونه‌برداری ثبت شده.",
  },
];

export const PANEL_ANALYTICS_GUIDE: GuideItem[] = [
  {
    title: "تازه‌سازی زنده",
    body: "مصرف را مستقیم از پنل‌ها می‌خواند، نمونهٔ جدید می‌سازد و جدول را درجا عوض می‌کند. اگر عددها قدیمی یا صفرند، همین دکمه را بزنید.",
    tone: "ok",
  },
  {
    title: "مصرف فعلی",
    body: "جمع خام used_traffic همهٔ اکانت‌های همان پنل — همیشه دقیق است، حتی قبل از پر شدن سطل ساعتی.",
    tone: "ok",
  },
  {
    title: "آنلاین کل / هر پنل",
    body: "کاربرانی که روی همان پنل اخیراً مصرف خام داشته‌اند (نه فقط باز کردن ساب).",
  },
  {
    title: "ساعت / امروز / هفته / ماه",
    body: "دلتای نمونه‌برداری دوره‌ای بدون ضریب. اولین نمونه فقط نقطهٔ شروع است؛ بعد از مصرف واقعی یا تازه‌سازی زنده پر می‌شود.",
  },
  {
    title: "نرخ و سرانه",
    body: "نرخ ≈ مصرف ساعت ÷ ۳۶۰۰. سرانه = نرخ ÷ تعداد آنلاین همان پنل.",
  },
  {
    title: "اپراتور / دستگاه",
    body: "تخمین از IP و User-Agent آخرین آپدیت ساب.",
  },
  {
    title: "تست پینگ",
    body: "از همگام‌سازی ping-hub: تعداد تست، top100، خوب، OK روی اپراتورها.",
  },
];
