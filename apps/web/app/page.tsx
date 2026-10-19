"use client";

import { useEffect, useState } from "react";
import { api, formatDuration, type Project } from "@/lib/api";
import { LENGTHS, StatusPill } from "./ui";

export default function Home() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [name, setName] = useState("");
  const [seconds, setSeconds] = useState(60);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.projects().then(setProjects).catch(() => setError("Could not reach the editor. Is it running?"));
  }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const p = await api.create(name, seconds);
      window.location.href = `/project/?id=${p.id}`;
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <main className="stack" style={{ gap: 28 }}>
      <section className="card stack">
        <div>
          <h1>Start a new vlog</h1>
          <p className="muted" style={{ margin: 0 }}>
            Drop in the clips from a shoot. Bidu picks the best moments, puts them in order and gives you a
            rough cut you can rearrange, preview and download.
          </p>
        </div>
        <form className="stack" onSubmit={create}>
          <label className="stack" style={{ gap: 6 }}>
            <span style={{ fontWeight: 600 }}>Name</span>
            <input type="text" placeholder="e.g. Saturday at the night market" value={name}
                   onChange={(e) => setName(e.target.value)} maxLength={120} />
          </label>
          <div className="stack" style={{ gap: 6 }}>
            <span style={{ fontWeight: 600 }}>How long should it be?</span>
            <div className="lengths" role="radiogroup">
              {LENGTHS.map((l) => (
                <button type="button" key={l} role="radio" aria-checked={seconds === l}
                        className={`length ${seconds === l ? "on" : ""}`} onClick={() => setSeconds(l)}>
                  {formatDuration(l)}
                </button>
              ))}
            </div>
          </div>
          <div>
            <button className="btn primary big" disabled={busy}>
              {busy ? "Creating…" : "Create and add clips"}
            </button>
          </div>
          {error && <div className="notice bad">{error}</div>}
        </form>
      </section>

      <section className="stack">
        <h2>Your vlogs</h2>
        {projects === null && !error && <p className="muted">Loading…</p>}
        {projects?.length === 0 && <p className="muted">Nothing yet. Your projects will show up here.</p>}
        <div className="projects">
          {projects?.map((p) => (
            <a key={p.id} className="card project-tile" href={`/project/?id=${p.id}`}>
              <div className="spread">
                <h3>{p.name}</h3>
                <StatusPill project={p} />
              </div>
              <span className="muted small">
                {p.assets.length} clip{p.assets.length === 1 ? "" : "s"} · {formatDuration(p.target_seconds)} target ·{" "}
                {new Date(p.created_at).toLocaleDateString()}
              </span>
            </a>
          ))}
        </div>
      </section>
    </main>
  );
}
