"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch, setToken, setOrgId } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import Link from "next/link";

export default function RegisterPage() {
  const router = useRouter();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [orgName, setOrgName] = useState("");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(""); setLoading(true);
    try {
      const data = await apiFetch("/auth/register", { method: "POST", body: JSON.stringify({ email, password, full_name: fullName, organization_name: orgName || null }) });
      setToken(data.access_token);
      const me = await apiFetch("/auth/me");
      if (me.memberships?.length) setOrgId(me.memberships[0].organization_id);
      router.push("/dashboard");
    } catch (e:any) { setErr(e.message);} finally { setLoading(false);}
  }
  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <Card className="w-full max-w-md">
        <CardHeader><CardTitle>ثبت‌نام در تصمیم‌یار</CardTitle><p className="text-sm text-muted-foreground">حساب و سازمان جدید بسازید</p></CardHeader>
        <form onSubmit={submit} className="space-y-4">
          <div className="space-y-2"><Label>نام و نام خانوادگی</Label><Input value={fullName} onChange={e=>setFullName(e.target.value)} required /></div>
          <div className="space-y-2"><Label>ایمیل</Label><Input value={email} onChange={e=>setEmail(e.target.value)} type="email" required /></div>
          <div className="space-y-2"><Label>رمز عبور (حداقل ۸ کاراکتر)</Label><Input value={password} onChange={e=>setPassword(e.target.value)} type="password" required /></div>
          <div className="space-y-2"><Label>نام سازمان (اختیاری — اگر خالی باشد بعداً می‌سازید)</Label><Input value={orgName} onChange={e=>setOrgName(e.target.value)} placeholder="مثلاً شرکت نمونه" /></div>
          {err && <div className="text-sm text-red-600 bg-red-50 p-2 rounded">{err}</div>}
          <Button className="w-full" disabled={loading}>{loading?"در حال ثبت‌نام...":"ثبت‌نام و ایجاد سازمان"}</Button>
          <div className="text-sm text-center">قبلاً ثبت‌نام کرده‌اید؟ <Link href="/login" className="text-[#0f2a44] font-medium">ورود</Link></div>
        </form>
      </Card>
    </div>
  );
}
