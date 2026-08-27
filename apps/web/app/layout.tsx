import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "تصمیم‌یار | داشبورد مدیریتی هوشمند",
  description: "داشبورد مدیریتی هوشمند چندمستاجره",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="fa" dir="rtl">
      <head>
        <link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;700&display=swap" rel="stylesheet" />
      </head>
      <body className="min-h-screen bg-[#f8f9fb]" style={{ fontFamily: "Vazirmatn, sans-serif" }}>
        {children}
      </body>
    </html>
  );
}
