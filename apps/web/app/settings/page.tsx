"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch, getOrgId, getToken } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import Link from "next/link";

const ROLES = ["owner","admin","manager","analyst","viewer"];

export default function SettingsPage(){
  const router = useRouter();
  const [orgId, setOrgIdState] = useState<string|null>(null);
  const [org, setOrg]=useState<any>(null);
  const [members, setMembers]=useState<any[]>([]);
  const [err,setErr]=useState("");
  const [inviteEmail,setInviteEmail]=useState("");
  const [inviteRole,setInviteRole]=useState("viewer");

  useEffect(()=>{
    const id = getOrgId();
    if (!getToken()) { router.push("/login"); return; }
    if (!id) { setErr("ابتدا یک سازمان را در داشبورد انتخاب کنید"); return; }
    setOrgIdState(id);
    apiFetch(`/organizations/${id}`).then(setOrg).catch(e=>setErr(e.message));
    apiFetch(`/organizations/${id}/members`).then(setMembers).catch(e=>setErr(e.message));
  },[]);

  async function invite(e: React.FormEvent){
    e.preventDefault();
    try{
      const m = await apiFetch(`/organizations/${orgId}/members/invite`, { method:"POST", body: JSON.stringify({ email: inviteEmail, role: inviteRole })});
      setMembers([...members, m]); setInviteEmail("");
    }catch(e:any){ setErr(e.message); }
  }
  async function changeRole(userId: string, role: string){
    try{
      const updated = await apiFetch(`/organizations/${orgId}/members/${userId}`, { method:"PATCH", body: JSON.stringify({ role })});
      setMembers(members.map(m=> m.user_id===userId ? updated : m));
    }catch(e:any){ setErr(e.message); }
  }
  async function remove(userId: string){
    if(!confirm("عضو حذف شود؟")) return;
    try{
      await apiFetch(`/organizations/${orgId}/members/${userId}`, { method:"DELETE"});
      setMembers(members.filter(m=>m.user_id!==userId));
    }catch(e:any){ setErr(e.message);}
  }

  if (err) return <div className="p-6"><div className="text-red-600 bg-red-50 p-3 rounded">{err}</div><Link href="/dashboard" className="text-[#0f2a44] text-sm mt-4 inline-block">→ بازگشت به داشبورد</Link></div>;
  if (!org) return <div className="p-6">در حال بارگذاری...</div>;

  return (
    <div className="min-h-screen bg-[#f8f9fb]">
      <header className="flex items-center justify-between border-b bg-white px-6 py-3">
        <div className="flex items-center gap-2"><Link href="/dashboard" className="text-sm text-[#0f2a44]">← داشبورد</Link><span className="font-bold text-[#0f2a44]">تنظیمات سازمان</span></div>
      </header>
      <div className="max-w-4xl mx-auto p-6 space-y-6">
        <div className="bg-white border rounded-xl p-6">
          <h1 className="text-xl font-bold text-[#0f2a44]">{org.name}</h1>
          <p className="text-sm text-muted-foreground">شناسه: {org.slug} — {org.id}</p>
        </div>

        <div className="bg-white border rounded-xl p-6">
          <h2 className="font-semibold mb-4">اعضای سازمان</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead><tr className="text-muted-foreground border-b"><th className="text-right p-2">نام</th><th className="text-right p-2">ایمیل</th><th className="text-right p-2">نقش</th><th className="p-2">عملیات</th></tr></thead>
              <tbody>
                {members.map(m=>(
                  <tr key={m.user_id} className="border-b">
                    <td className="p-2">{m.full_name}</td>
                    <td className="p-2">{m.email}</td>
                    <td className="p-2">
                      <select value={m.role} onChange={e=>changeRole(m.user_id, e.target.value)} className="border rounded px-2 py-1 text-sm">
                        {ROLES.map(r=><option key={r} value={r}>{r}</option>)}
                      </select>
                    </td>
                    <td className="p-2"><Button variant="ghost" onClick={()=>remove(m.user_id)} className="text-red-600">حذف</Button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <form onSubmit={invite} className="bg-white border rounded-xl p-6 space-y-3">
          <h3 className="font-semibold">دعوت عضو جدید</h3>
          <p className="text-sm text-muted-foreground">کاربر باید قبلاً ثبت‌نام کرده باشد. ایمیل او را وارد کنید.</p>
          <div className="flex gap-2">
            <Input placeholder="email@example.com" value={inviteEmail} onChange={e=>setInviteEmail(e.target.value)} required />
            <select value={inviteRole} onChange={e=>setInviteRole(e.target.value)} className="border rounded-lg px-3 h-10 text-sm bg-white">
              {ROLES.filter(r=>r!=="owner").map(r=><option key={r} value={r}>{r}</option>)}
            </select>
            <Button type="submit">دعوت</Button>
          </div>
        </form>
      </div>
    </div>
  );
}
