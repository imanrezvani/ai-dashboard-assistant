"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch, setToken, setOrgId } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import Link from "next/link";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(""); setLoading(true);
    try {
      const data = await apiFetch("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
      setToken(data.access_token);
      // fetch me to get org id
      const me = await apiFetch("/auth/me", { method: "GET" });
      if (me.memberships?.length) setOrgId(me.memberships[0].organization_id);
      router.push("/dashboard");
    } catch (e: any) { setErr(e.message); } finally { setLoading(false); }
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <Card className="w-full max-w-md">
        <CardHeader><CardTitle>ورود به تصمیم‌یار</CardTitle><p className="text-sm text-muted-foreground">ایمیل و رمز عبور خود را وارد کنید</p></CardHeader>
        <form onSubmit={submit} className="space-y-4">
          <div className="space-y-2"><Label>ایمیل</Label><Input value={email} onChange={e=>setEmail(e.target.value)} placeholder="you@company.com" type="email" required /></div>
          <div className="space-y-2"><Label>رمز عبور</Label><Input value={password} onChange={e=>setPassword(e.target.value)} type="password" required /></div>
          {err && <div className="text-sm text-red-600 bg-red-50 p-2 rounded">{err}</div>}
          <Button className="w-full" disabled={loading}>{loading?"در حال ورود...":"ورود"}</Button>
          <div className="text-sm text-center">حساب ندارید؟ <Link href="/register" className="text-[#0f2a44] font-medium">ثبت‌نام</Link></div>
        </form>
      </Card>
    </div>
  );
}
