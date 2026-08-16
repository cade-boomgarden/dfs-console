import { FormEvent, useState } from "react";
import { api } from "../api";
import { Btn, Field } from "../ui";

export default function Login({ onSuccess }: { onSuccess: () => void }) {
  const [username, setU] = useState("");
  const [password, setP] = useState("");
  const [error, setError] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await api.post("/api/auth/login", { username, password });
      onSuccess();
    } catch (err) {
      setError((err as Error).message);
    }
  };
  return (
    <div className="h-screen flex items-center justify-center">
      <form onSubmit={submit} className="panel p-6 w-72 space-y-4">
        <div className="tracking-widest text-[var(--amber)] font-semibold">DFS·CONSOLE</div>
        <Field label="Username"><input value={username} onChange={(e) => setU(e.target.value)} autoFocus /></Field>
        <Field label="Password"><input type="password" value={password} onChange={(e) => setP(e.target.value)} /></Field>
        {error && <div className="text-xs text-[var(--down)]">{error}</div>}
        <Btn kind="primary" type="submit">Sign in</Btn>
        <div className="text-[11px] text-[var(--dim)]">Accounts are seeded by script — no signup.</div>
      </form>
    </div>
  );
}
