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
    title: "مصرف خام / فعلی",
    body: "جمع واقعی used_traffic اکانت‌ها بدون ضریب. مصرف فعلی همیشه دقیق است.",
    tone: "ok",
  },
  {
    title: "مقایسه ساعت/روز/هفته/ماه",
    body: "بازهٔ محلی دستگاه + تناسب سطل‌های ساعتی؛ مصرف تازه‌ای که هنوز نمونه‌نشده هم (فقط بعد از seed) اضافه می‌شود.",
  },
  {
    title: "نمودار ساعتی",
    body: "مصرف هر ساعت در ۴۸ ساعت اخیر با برچسب زمان محلی دستگاه.",
  },
  {
    title: "ساعات روز",
    body: "در کدام ساعت‌های شبانه‌روز (محلی) بیشتر مصرف داشته. ستون طلایی = اوج.",
  },
  {
    title: "دستگاه / اینترنت",
    body: "از درخواست آپدیت لینک ساب: اپ کلاینت و شبکه‌های اینترنت متمایز (/24).",
  },
];

export const PANEL_ANALYTICS_GUIDE: GuideItem[] = [
  {
    title: "تازه‌سازی زنده",
    body: "مصرف را مستقیم از پنل‌ها می‌خواند و آمار را درجا عوض می‌کند.",
    tone: "ok",
  },
  {
    title: "بدون ضریب / با ضریب",
    body: "بدون ضریب = جمع خام اکانت‌های Astra. با ضریب کل = همان «ترافیک مصرفی» داشبورد.",
    tone: "ok",
  },
  {
    title: "نمودار ۲۴ ساعت",
    body: "فقط وقتی نمونه‌برداری ساعتی واقعی باشد نشان داده می‌شود؛ با ضریب هر پنل.",
    tone: "ok",
  },
  {
    title: "جدول پنل‌ها",
    body: "فقط مصرف فعلی دقیق (خام و با ضریب) و تعداد اکانت. آنلاین/ساعت/امروز به‌خاطر عدم دقت حذف شده‌اند.",
  },
  {
    title: "جزئیات پنل",
    body: "کلیک روی نوار سهم یا ردیف جدول، جزئیات را در مدال باز می‌کند.",
  },
  {
    title: "تست پینگ",
    body: "فقط وقتی دادهٔ واقعی از ping-hub باشد در مدال نشان داده می‌شود.",
  },
];
