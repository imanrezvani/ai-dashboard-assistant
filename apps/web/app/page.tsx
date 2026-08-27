import Link from "next/link";
import { Button } from "@/components/ui/button";

export default function Home() {
  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex items-center justify-between border-b bg-white px-6 py-4">
        <div className="flex items-center gap-2">
          <div className="h-8 w-8 rounded-lg bg-[#0f2a44] flex items-center justify-center text-white font-bold">ت</div>
          <span className="font-bold text-[#0f2a44] text-lg">تصمیم‌یار</span>
        </div>
        <div className="flex gap-2">
          <Link href="/login"><Button variant="ghost">ورود</Button></Link>
          <Link href="/register"><Button>ثبت‌نام</Button></Link>
        </div>
      </header>
      <main className="flex flex-1 flex-col items-center justify-center px-6 text-center">
        <h1 className="text-4xl font-bold text-[#0f2a44] mb-4">داشبورد مدیریتی هوشمند</h1>
        <p className="text-muted-foreground max-w-xl mb-8">داده‌های کسب‌وکار خود را از منابع مختلف بخوانید، KPIها را ببینید و با دستیار هوش مصنوعی تحلیل کنید. هر سازمان داده‌ای کاملاً ایزوله دارد.</p>
        <div className="flex gap-3">
          <Link href="/register"><Button>شروع کنید — رایگان</Button></Link>
          <Link href="/login"><Button variant="outline">ورود به حساب</Button></Link>
        </div>
        <div className="mt-12 grid grid-cols-1 md:grid-cols-3 gap-4 max-w-3xl w-full text-right">
          {[
            { t: "چندمستاجری امن", d: "ایزولاسیون کامل داده با RLS در PostgreSQL" },
            { t: "نقش‌های دقیق", d: "Owner / Admin / Manager / Analyst / Viewer" },
            { t: "رابط فارسی RTL", d: "طراحی مدرن با رنگ آبی تیره حرفه‌ای" },
          ].map(c=>(
            <div key={c.t} className="rounded-xl border bg-white p-5">
              <div className="font-semibold text-[#0f2a44]">{c.t}</div>
              <div className="text-sm text-muted-foreground mt-1">{c.d}</div>
            </div>
          ))}
        </div>
      </main>
    </div>
  );
}
