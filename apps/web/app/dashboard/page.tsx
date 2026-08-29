"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch, clearToken, getToken, getOrgId, setOrgId, setToken } from "@/lib/api";
import { Button } from "@/components/ui/button";
import Link from "next/link";

export default function Dashboard() {
  const router = useRouter();
  const [me, setMe] = useState<any>(null);
  const [err, setErr] = useState("");

  useEffect(()=>{
    if (!getToken()) { router.push("/login"); return; }
    apiFetch("/auth/me").then(setMe).catch(e=>setErr(e.message));
  },[]);

  if (err) return <div className="p-6 text-red-600">{err} <Button onClick={()=>{clearToken();router.push("/login")}}>ورود مجدد</Button></div>;
  if (!me) return <div className="p-6">در حال بارگذاری...</div>;

  async function switchOrg(id: string) {
    const data = await apiFetch(`/auth/switch-organization?organization_id=${id}`, { method: "POST" });
    setToken(data.access_token);
    setOrgId(id);
    location.reload();
  }

  const currentOrgId = getOrgId();

  return (
    <div className="min-h-screen">
      <header className="flex items-center justify-between border-b bg-white px-6 py-3 sticky top-0">
        <div className="flex items-center gap-3">
          <div className="h-8 w-8 rounded-lg bg-[#0f2a44] flex items-center justify-center text-white font-bold">ت</div>
          <span className="font-bold text-[#0f2a44]">تصمیم‌یار</span>
          <span className="text-sm text-muted-foreground">— داشبورد</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-sm">{me.user.full_name} ({me.user.email})</span>
          <Link href="/data-sources"><Button variant="outline">منابع داده</Button></Link>
          <Link href="/settings"><Button variant="outline">تنظیمات سازمان</Button></Link>
          <Button variant="ghost" onClick={()=>{clearToken();router.push("/login")}}>خروج</Button>
        </div>
      </header>

      <div className="p-6 max-w-5xl mx-auto space-y-6">
        <div className="rounded-xl bg-[#0f2a44] text-white p-6">
          <h1 className="text-2xl font-bold">خوش آمدید، {me.user.full_name} 👋</h1>
          <p className="opacity-80 mt-2 text-sm">از اینجا سازمان خود را مدیریت کنید. داشبورد KPI و دستیار هوش مصنوعی در مرحله بعد اضافه می‌شود.</p>
        </div>

        <div className="grid gap-4">
          <h2 className="font-semibold">سازمان‌های شما</h2>
          {me.memberships.length===0 && <div className="text-sm text-muted-foreground border rounded-lg p-4 bg-white">هنوز عضو هیچ سازمانی نیستید. یک سازمان جدید بسازید.</div>}
          {me.memberships.map((m:any)=>(
            <div key={m.organization_id} className={`flex items-center justify-between border rounded-xl p-4 bg-white ${currentOrgId===m.organization_id?"ring-2 ring-[#0f2a44]":""}`}>
              <div>
                <div className="font-medium">{m.organization_name} <span className="text-xs text-muted-foreground">({m.organization_slug})</span></div>
                <div className="text-sm text-muted-foreground">نقش شما: {m.role}</div>
              </div>
              <div className="flex gap-2">
                {currentOrgId!==m.organization_id && <Button variant="outline" onClick={()=>switchOrg(m.organization_id)}>انتخاب</Button>}
                {currentOrgId===m.organization_id && <span className="text-sm text-green-700 bg-green-50 px-3 py-1 rounded-full">فعال</span>}
              </div>
            </div>
          ))}
        </div>

        <CreateOrg onCreated={()=>location.reload()} />

        <div className="border rounded-xl bg-white p-6">
          <h3 className="font-semibold mb-2">ایزولاسیون چندمستاجری</h3>
          <p className="text-sm text-muted-foreground">تمام درخواست‌ها دارای هدر <code className="bg-muted px-1 rounded">X-Organization-Id</code> و توکن JWT حاوی <code className="bg-muted px-1 rounded">org_id</code> هستند و سرور در هر query فیلتر <code className="bg-muted px-1 rounded">organization_id</code> را اعمال می‌کند. جدول <code>memberships</code> دارای Row-Level Security در PostgreSQL است.</p>
        </div>
      </div>
    </div>
  );
}

function CreateOrg({ onCreated }: { onCreated: ()=>void }) {
  const [name, setName]=useState(""); const [slug,setSlug]=useState(""); const [msg,setMsg]=useState("");
  async function submit(e: React.FormEvent){
    e.preventDefault(); setMsg("");
    try{
      await apiFetch("/organizations", { method:"POST", body: JSON.stringify({ name, slug: slug || name.toLowerCase().replace(/\s+/g,"-") })});
      setMsg("سازمان ساخته شد"); onCreated();
    }catch(e:any){setMsg(e.message);}
  }
  return (
    <form onSubmit={submit} className="border rounded-xl bg-white p-4 flex flex-col gap-3">
      <div className="font-medium">ساخت سازمان جدید</div>
      <div className="flex gap-2">
        <input className="flex h-10 w-full rounded-lg border px-3 text-sm" placeholder="نام سازمان" value={name} onChange={e=>setName(e.target.value)} required />
        <input className="flex h-10 w-full rounded-lg border px-3 text-sm" placeholder="slug (اختیاری)" value={slug} onChange={e=>setSlug(e.target.value)} />
        <Button type="submit">ایجاد</Button>
      </div>
      {msg && <div className="text-sm text-muted-foreground">{msg}</div>}
    </form>
  )
}
