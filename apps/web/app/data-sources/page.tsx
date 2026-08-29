"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { apiFetch, getOrgId, getToken } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useRouter } from "next/navigation";

type Preview = {
  id: string;
  name: string;
  file_type: string;
  row_count: number;
  status: string;
  columns: string[];
  preview: Record<string, any>[];
};

export default function DataSourcesPage() {
  const router = useRouter();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [mapping, setMapping] = useState({ measure_column: "", date_column: "", category_column: "", label_column: "" });
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [sources, setSources] = useState<any[]>([]);

  useEffect(() => {
    if (!getToken()) { router.push("/login"); return; }
    if (!getOrgId()) { setErr("ابتدا سازمان را در داشبورد انتخاب کنید"); return; }
    apiFetch("/data-sources").then(setSources).catch(e => setErr(e.message));
  }, []);

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault();
    if (!file) { setErr("فایلی انتخاب نشده"); return; }
    setErr(""); setMsg(""); setLoading(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      // apiFetch handles FormData without JSON header
      const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
      const token = getToken();
      const orgId = getOrgId();
      const res = await fetch(`${API_URL}/data-sources/upload`, {
        method: "POST",
        headers: {
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          ...(orgId ? { "X-Organization-Id": orgId } : {}),
        },
        body: fd,
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(d.detail || "خطا در آپلود");
      }
      const data: Preview = await res.json();
      setPreview(data);
      setMapping({ measure_column: data.columns[0] || "", date_column: "", category_column: "", label_column: "" });
      setMsg(`پیش‌نمایش آماده: ${data.row_count} ردیف، ستون‌ها: ${data.columns.join(", ")}`);
      // refresh list
      apiFetch("/data-sources").then(setSources).catch(() => {});
    } catch (e: any) { setErr(e.message); } finally { setLoading(false); }
  }

  async function handleMap() {
    if (!preview) return;
    if (!mapping.measure_column) { setErr("ستون measure الزامی است"); return; }
    setErr(""); setLoading(true);
    try {
      const data = await apiFetch(`/data-sources/${preview.id}/map`, {
        method: "POST",
        body: JSON.stringify({
          measure_column: mapping.measure_column,
          date_column: mapping.date_column || null,
          category_column: mapping.category_column || null,
          label_column: mapping.label_column || null,
        }),
      });
      setMsg(`ذخیره شد: ${data.inserted_rows} ردیف در fact_rows`);
      setPreview(null);
      apiFetch("/data-sources").then(setSources).catch(() => {});
    } catch (e: any) { setErr(e.message); } finally { setLoading(false); }
  }

  return (
    <div className="min-h-screen bg-[#f8f9fb]">
      <header className="flex items-center justify-between border-b bg-white px-6 py-3">
        <div className="flex items-center gap-2">
          <Link href="/dashboard" className="text-sm text-[#0f2a44]">← داشبورد</Link>
          <span className="font-bold text-[#0f2a44]">منابع داده</span>
        </div>
      </header>

      <div className="max-w-5xl mx-auto p-6 space-y-6">
        <Card>
          <CardHeader><CardTitle>آپلود فایل CSV / Excel</CardTitle><p className="text-sm text-muted-foreground">فایل را آپلود کنید، پیش‌نمایش ستون‌ها و ۱۰ ردیف اول را ببینید، سپس Mapping را انجام دهید</p></CardHeader>
          <form onSubmit={handleUpload} className="space-y-4">
            <div className="space-y-2">
              <Label>انتخاب فایل</Label>
              <Input type="file" accept=".csv,.xlsx,.xls" onChange={e => setFile(e.target.files?.[0] || null)} />
            </div>
            <Button type="submit" disabled={loading || !file}>{loading ? "در حال آپلود..." : "پیش‌نمایش"}</Button>
          </form>
          {err && <div className="text-sm text-red-600 bg-red-50 p-2 rounded mt-4">{err}</div>}
          {msg && <div className="text-sm text-green-700 bg-green-50 p-2 rounded mt-4">{msg}</div>}
        </Card>

        {preview && (
          <Card>
            <CardHeader><CardTitle>پیش‌نمایش: {preview.name} — {preview.row_count} ردیف</CardTitle></CardHeader>
            <div className="overflow-auto">
              <table className="w-full text-sm">
                <thead><tr className="bg-muted">{preview.columns.map(c => <th key={c} className="p-2 text-right border">{c}</th>)}</tr></thead>
                <tbody>{preview.preview.map((row, i) => <tr key={i} className="border-b">{preview.columns.map(c => <td key={c} className="p-2 border text-xs">{String(row[c] ?? "")}</td>)}</tr>)}</tbody>
              </table>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-6">
              {[
                { key: "measure_column", label: "ستون measure (عددی) *", required: true },
                { key: "date_column", label: "ستون تاریخ (dimension_date)" },
                { key: "category_column", label: "ستون دسته‌بندی (dimension_category)" },
                { key: "label_column", label: "ستون برچسب (dimension_label)" },
              ].map(f => (
                <div key={f.key} className="space-y-1">
                  <Label>{f.label}</Label>
                  <select
                    className="flex h-10 w-full rounded-lg border bg-white px-3 text-sm"
                    value={(mapping as any)[f.key]}
                    onChange={e => setMapping({ ...mapping, [f.key]: e.target.value })}
                  >
                    <option value="">— انتخاب نکن —</option>
                    {preview.columns.map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
              ))}
            </div>
            <div className="mt-4 flex gap-2">
              <Button onClick={handleMap} disabled={loading}>تأیید و ذخیره در fact_rows</Button>
              <Button variant="outline" onClick={() => setPreview(null)}>انصراف</Button>
            </div>
          </Card>
        )}

        <Card>
          <CardHeader><CardTitle>منابع ثبت‌شده</CardTitle></CardHeader>
          {sources.length === 0 ? <div className="text-sm text-muted-foreground">هنوز منبعی ثبت نشده</div> :
            <table className="w-full text-sm">
              <thead><tr className="text-muted-foreground border-b"><th className="text-right p-2">نام</th><th className="p-2">نوع</th><th className="p-2">تعداد ردیف</th><th className="p-2">وضعیت</th><th className="p-2">تاریخ</th></tr></thead>
              <tbody>{sources.map((s: any) => <tr key={s.id} className="border-b"><td className="p-2">{s.name}</td><td className="p-2">{s.file_type}</td><td className="p-2">{s.row_count}</td><td className="p-2">{s.status}</td><td className="p-2 text-xs">{new Date(s.uploaded_at).toLocaleString("fa-IR")}</td></tr>)}</tbody>
            </table>
          }
        </Card>
      </div>
    </div>
  );
}
