"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  api,
  at30fps,
  formatClock,
  formatDuration,
  uploadFile,
  type Project,
  type Shot,
  type Storyboard,
} from "@/lib/api";
import { LENGTHS, StatusPill, isWorking } from "../ui";

const VIDEO_EXT = /\.(mp4|mov|m4v)$/i;

type LocalUpload = { key: string; name: string; progress: number; failed?: string };

export default function ProjectPageWrapper() {
  return (
    <Suspense fallback={<p className="muted">Loading…</p>}>
      <ProjectPage />
    </Suspense>
  );
}

function ProjectPage() {
  const id = useSearchParams().get("id") ?? "";
  const [project, setProject] = useState<Project | null>(null);
  const [board, setBoard] = useState<Storyboard | null>(null);
  const [uploads, setUploads] = useState<LocalUpload[]>([]);
  const [seconds, setSeconds] = useState(60);
  const [toast, setToast] = useState<string | null>(null);
  const [watching, setWatching] = useState<Shot | null>(null);
  const [notFound, setNotFound] = useState(false);
  const boardEdit = useRef<string | null>(null);

  const say = useCallback((msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast(null), 4000);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const p = await api.project(id);
      setProject(p);
      if (p.current_edit_id && p.current_edit_id !== boardEdit.current) {
        const b = await api.storyboard(id);
        boardEdit.current = b.edit_id;
        setBoard(b);
      } else if (board && p.preview_is_current !== board.preview_is_current) {
        setBoard({ ...board, preview_is_current: p.preview_is_current });
      }
      return p;
    } catch (e) {
      if ((e as { status?: number }).status === 404) setNotFound(true);
      return null;
    }
  }, [id, board]);

  useEffect(() => {
    refresh().then((p) => p && setSeconds(p.target_seconds));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const pendingUploads = uploads.some((u) => u.progress < 1 && !u.failed);
  useEffect(() => {
    if (!isWorking(project) && !pendingUploads) return;
    const t = window.setInterval(refresh, 1200);
    return () => window.clearInterval(t);
  }, [project, pendingUploads, refresh]);

  async function addFiles(files: File[]) {
    const videos = files.filter((f) => VIDEO_EXT.test(f.name));
    const skipped = files.length - videos.length;
    if (skipped) say(`Skipped ${skipped} file${skipped === 1 ? "" : "s"} that aren't MP4 or MOV videos.`);
    const batch = videos.map((f, i) => ({ key: `${Date.now()}-${i}-${f.name}`, name: f.name, progress: 0 }));
    setUploads((u) => [...u, ...batch]);
    const queue = videos.map((f, i) => [f, batch[i].key] as const);
    const worker = async () => {
      for (let next = queue.shift(); next; next = queue.shift()) {
        const [file, key] = next;
        try {
          await uploadFile(id, file, (p) =>
            setUploads((u) => u.map((x) => (x.key === key ? { ...x, progress: p } : x))),
          );
          setUploads((u) => u.filter((x) => x.key !== key));
          refresh();
        } catch (e) {
          setUploads((u) => u.map((x) => (x.key === key ? { ...x, failed: (e as Error).message } : x)));
        }
      }
    };
    await Promise.all([worker(), worker()]);
  }

  async function run<T>(fn: () => Promise<T>, done?: (v: T) => void) {
    try {
      const v = await fn();
      done?.(v);
      await refresh();
    } catch (e) {
      say((e as Error).message);
      if ((e as { code?: string }).code === "STALE_REVISION") {
        boardEdit.current = null;
        refresh();
      }
    }
  }

  function edit(ops: object[]) {
    if (!board) return;
    run(() => api.edit(id, board.edit_id, ops), (b) => {
      boardEdit.current = b.edit_id;
      setBoard(b);
    });
  }

  if (notFound) return <p className="muted">This project doesn't exist. <a href="/">Go back</a></p>;
  if (!project) return <p className="muted">Loading…</p>;

  const usable = project.assets.filter((a) => a.status === "ready").length;
  const checking = project.assets.filter((a) => a.status === "checking").length;
  const job = project.job && !project.job.finished_at ? project.job : null;
  const hasCut = Boolean(board);
  const makeBusy = job?.kind === "make";

  return (
    <main className="stack" style={{ gap: 20 }}>
      <div className="spread">
        <div>
          <a href="/" className="muted small">← All vlogs</a>
          <h1>{project.name}</h1>
        </div>
        <StatusPill project={project} />
      </div>

      {/* Step 1: clips */}
      <section className="card">
        <StepHead n={1} done={usable > 0} active={!hasCut} title="Add your clips"
                  hint={usable ? `${usable} clip${usable === 1 ? "" : "s"} ready` : "MP4 or MOV, straight from your phone"} />
        <DropZone onFiles={addFiles} />
        {(uploads.length > 0 || project.assets.length > 0) && (
          <div className="files">
            {uploads.map((u) => (
              <div className="file" key={u.key}>
                <span className="file-name">{u.name}</span>
                <span className="muted small">{u.failed ? "" : `Uploading ${Math.round(u.progress * 100)}%`}</span>
                {u.failed ? <span className="file-err">{u.failed}</span> : (
                  <div className="bar" style={{ gridColumn: "1 / -1" }}><span style={{ width: `${u.progress * 100}%` }} /></div>
                )}
              </div>
            ))}
            {project.assets.map((a) => (
              <div className="file" key={a.id}>
                <span className="file-name">{a.file}</span>
                <span className="row">
                  {a.status === "ready" && <span className="pill good">Ready</span>}
                  {a.status === "checking" && <span className="pill accent">Checking…</span>}
                  {a.status === "needs_attention" && <span className="pill bad">Can't use</span>}
                  {!job && (
                    <button className="btn tiny ghost" title="Remove from this vlog"
                            onClick={() => run(() => api.removeFile(id, a.id))}>✕</button>
                  )}
                </span>
                {a.error && <span className="file-err">{a.error}</span>}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Step 2: make */}
      <section className="card">
        <StepHead n={2} done={hasCut} active={usable > 0 && !hasCut} title="Make the rough cut"
                  hint="Pick a length. You can make it again any time." />
        <div className="stack">
          <div className="lengths">
            {LENGTHS.map((l) => (
              <button key={l} className={`length ${seconds === l ? "on" : ""}`} onClick={() => setSeconds(l)}>
                {formatDuration(l)}
              </button>
            ))}
          </div>
          <div className="row">
            <button className="btn primary big" disabled={!usable || checking > 0 || Boolean(job) || pendingUploads}
                    onClick={() => run(() => api.make(id, seconds), () => { boardEdit.current = null; })}>
              {hasCut ? "Make a new rough cut" : "Make my rough cut"}
            </button>
            {checking > 0 && <span className="muted small">Waiting for {checking} clip{checking === 1 ? "" : "s"} to finish checking…</span>}
          </div>
          {makeBusy && job && <Progress stage={job.stage} completed={job.completed} total={job.total} />}
          {project.status === "needs_attention" && project.job?.error && (
            <div className="notice bad">{project.job.error}</div>
          )}
          {project.notes.filter((n) => !n.startsWith("Review:")).map((n) => (
            <div key={n} className="notice warn">{n}</div>
          ))}
        </div>
      </section>

      {/* Step 3: watch and edit */}
      {board && (
        <section className="card stack">
          <StepHead n={3} done={false} active title="Watch and fix it up"
                    hint={`${formatDuration(board.duration_s)} · version ${board.version}`} />
          <div style={{ position: "relative" }}>
            {project.preview_edit_id ? (
              <video key={project.preview_edit_id} className="player" controls playsInline preload="metadata"
                     poster={board.beats[0]?.shots[0]?.thumb_url}
                     src={`/v1/projects/${id}/preview.mp4?v=${project.preview_edit_id}`} />
            ) : (
              <div className="player" style={{ display: "grid", placeItems: "center", color: "#fff" }}>Rendering…</div>
            )}
          </div>
          {!project.preview_is_current && (
            <div className="row muted small"><span className="pill accent">Updating</span> The preview is catching up with your latest changes.</div>
          )}
          <div className="spread">
            <p className="muted small" style={{ margin: 0 }}>
              Move sections, remove shots, or make them shorter or longer. Lock a shot to keep it when you make a new cut.
            </p>
            <button className="btn" disabled={!board.can_undo || makeBusy} onClick={() =>
              run(() => api.undo(id), (b) => { boardEdit.current = b.edit_id; setBoard(b); })}>
              ↶ Undo
            </button>
          </div>
          {project.notes.filter((n) => n.startsWith("Review:")).map((n) => (
            <div key={n} className="notice info">{n.replace("Review: ", "Check this: ")}</div>
          ))}
          <div className="stack" style={{ gap: 20 }}>
            {board.beats.map((beat, bi) => (
              <div className="beat" key={beat.id}>
                <div className="spread">
                  <div>
                    <h3>{beat.title} <span className="muted small">· {formatDuration(beat.duration_s)}</span></h3>
                    {beat.purpose && <span className="muted small">{beat.purpose}</span>}
                  </div>
                  <div className="row" style={{ gap: 4 }}>
                    <button className="btn tiny" disabled={bi === 0 || makeBusy} title="Move this section earlier"
                            onClick={() => edit([{ op: "move_card", beat_id: beat.id, to_index: bi - 1 }])}>↑ Earlier</button>
                    <button className="btn tiny" disabled={bi === board.beats.length - 1 || makeBusy} title="Move this section later"
                            onClick={() => edit([{ op: "move_card", beat_id: beat.id, to_index: bi + 1 }])}>↓ Later</button>
                    <button className="btn tiny danger" disabled={board.beats.length === 1 || makeBusy}
                            onClick={() => edit([{ op: "remove_card", beat_id: beat.id }])}>Remove section</button>
                  </div>
                </div>
                <div className="shots">
                  {beat.shots.map((s) => (
                    <ShotCard key={s.clip_id} shot={s} disabled={makeBusy} onWatch={() => setWatching(s)} onEdit={edit}
                              onlyShot={board.beats.length === 1 && beat.shots.filter((x) => x.kind === "main").length === 1} />
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Step 4: download */}
      {board && (
        <section className="card">
          <StepHead n={4} done={project.has_export} active={project.preview_is_current} title="Download"
                    hint="Full HD 1080p MP4 of the current version" />
          <div className="stack">
            <div className="row">
              {project.has_export ? (
                <a className="btn primary big" href={`/v1/projects/${id}/download`}>⬇ Download video</a>
              ) : (
                <button className="btn primary big" disabled={Boolean(job)} onClick={() => run(() => api.exportHd(id))}>
                  Make HD video
                </button>
              )}
              {project.has_export && (
                <span className="muted small">This is the version you're looking at. Edit again and you'll make a new one.</span>
              )}
            </div>
            {job?.kind === "export" && <Progress stage={job.stage} completed={job.completed} total={job.total} />}
          </div>
        </section>
      )}

      {watching && <SourceModal shot={watching} onClose={() => setWatching(null)} />}
      {toast && <div className="toast" role="status">{toast}</div>}
    </main>
  );
}

function StepHead({ n, done, active, title, hint }: { n: number; done: boolean; active: boolean; title: string; hint: string }) {
  return (
    <div className="step-head">
      <span className={`step-num ${done ? "done" : active ? "active" : ""}`}>{done ? "✓" : n}</span>
      <div>
        <h2>{title}</h2>
        <span className="muted small">{hint}</span>
      </div>
    </div>
  );
}

function Progress({ stage, completed, total }: { stage: string; completed: number; total: number }) {
  const known = total > 1;
  return (
    <div className="stack" style={{ gap: 6 }}>
      <div className="spread small">
        <span style={{ fontWeight: 600 }}>{stage}…</span>
        {known && <span className="muted">{completed} of {total}</span>}
      </div>
      <div className={`bar ${known ? "" : "indeterminate"}`}>
        <span style={{ width: known ? `${(completed / total) * 100}%` : undefined }} />
      </div>
    </div>
  );
}

function ShotCard({ shot, disabled, onWatch, onEdit, onlyShot }: {
  shot: Shot; disabled: boolean; onWatch: () => void; onEdit: (ops: object[]) => void; onlyShot: boolean;
}) {
  const trim = (startDelta: number, endDelta: number) => {
    const inS = Math.max(0, shot.source_in_s + startDelta);
    const outS = shot.source_out_s + endDelta;
    onEdit([{ op: "trim_clip", clip_id: shot.clip_id, source_in: at30fps(inS), source_duration: at30fps(outS - inS) }]);
  };
  const tooShort = shot.duration_s <= 1.2;
  return (
    <div className={`shot ${shot.kind === "cutaway" ? "cutaway" : ""} ${shot.locked ? "locked" : ""}`}>
      <div className="thumb">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={shot.thumb_url} alt="" loading="lazy"
             onError={(e) => {
               // Thumbnails are made on first request; try once more if that one raced.
               const img = e.currentTarget;
               if (!img.dataset.retried) { img.dataset.retried = "1"; setTimeout(() => (img.src = `${shot.thumb_url}&r=1`), 800); }
             }} />
        <span className="badge">
          {shot.kind === "cutaway" ? <span className="pill">Cutaway</span> : shot.locked ? <span className="pill accent">🔒 Locked</span> : null}
        </span>
        <span className="dur">{shot.duration_s.toFixed(1)}s</span>
      </div>
      <div className="shot-body">
        <span className="shot-file" title={shot.file}>{shot.file} · {formatClock(shot.source_in_s)}</span>
        {shot.quote ? <span className="shot-why">“{shot.quote}”</span> : shot.why && <span className="shot-why">{shot.why}</span>}
        <div className="shot-tools">
          <button className="btn tiny" onClick={onWatch}>▶ Original</button>
          {shot.kind === "main" && !shot.locked && (
            <>
              <button className="btn tiny" disabled={disabled || tooShort} onClick={() => trim(0, -1)} title="Cut 1 second off the end">−1s</button>
              <button className="btn tiny" disabled={disabled} onClick={() => trim(0, 1)} title="Add 1 second at the end">+1s</button>
            </>
          )}
          {shot.kind === "main" && (
            <button className="btn tiny" disabled={disabled}
                    onClick={() => onEdit([{ op: "set_lock", clip_id: shot.clip_id, source: !shot.locked }])}>
              {shot.locked ? "Unlock" : "Lock"}
            </button>
          )}
          {!shot.locked && (
            <button className="btn tiny danger" disabled={disabled || onlyShot}
                    onClick={() => onEdit([{ op: "remove_clip", clip_id: shot.clip_id }])}>Remove</button>
          )}
        </div>
      </div>
    </div>
  );
}

function SourceModal({ shot, onClose }: { shot: Shot; onClose: () => void }) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-back" onClick={onClose}>
      <div className="card modal stack" onClick={(e) => e.stopPropagation()}>
        <div className="spread">
          <div>
            <h3>{shot.file}</h3>
            <span className="muted small">
              The shot uses {formatClock(shot.source_in_s)} to {formatClock(shot.source_out_s)} of this clip.
            </span>
          </div>
          <button className="btn" onClick={onClose}>Close</button>
        </div>
        <video ref={ref} className="player" controls autoPlay playsInline
               src={`${shot.source_url}#t=${shot.source_in_s},${shot.source_out_s}`} />
        {shot.why && <span className="muted small">Why this shot: {shot.why}</span>}
      </div>
    </div>
  );
}

function DropZone({ onFiles }: { onFiles: (files: File[]) => void }) {
  const [over, setOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    folderInput.current?.setAttribute("webkitdirectory", "");
  }, []);

  async function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setOver(false);
    const entries = Array.from(e.dataTransfer.items)
      .map((i) => i.webkitGetAsEntry?.())
      .filter(Boolean) as FileSystemEntry[];
    if (entries.length) onFiles(await readEntries(entries));
    else onFiles(Array.from(e.dataTransfer.files));
  }

  return (
    <div className={`drop ${over ? "over" : ""}`}
         onDragOver={(e) => { e.preventDefault(); setOver(true); }}
         onDragLeave={() => setOver(false)} onDrop={onDrop}>
      <p style={{ margin: "0 0 12px", fontWeight: 600 }}>Drag a folder or clips here</p>
      <div className="row" style={{ justifyContent: "center" }}>
        <button className="btn" onClick={() => fileInput.current?.click()}>Choose clips</button>
        <button className="btn" onClick={() => folderInput.current?.click()}>Choose a folder</button>
      </div>
      <input ref={fileInput} type="file" multiple accept="video/mp4,video/quicktime,.mp4,.mov,.m4v" hidden
             onChange={(e) => { onFiles(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
      <input ref={folderInput} type="file" multiple hidden
             onChange={(e) => { onFiles(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
    </div>
  );
}

async function readEntries(entries: FileSystemEntry[]): Promise<File[]> {
  const out: File[] = [];
  for (const entry of entries) {
    if (entry.isFile) {
      out.push(await new Promise<File>((res, rej) => (entry as FileSystemFileEntry).file(res, rej)));
    } else if (entry.isDirectory) {
      const reader = (entry as FileSystemDirectoryEntry).createReader();
      let batch: FileSystemEntry[];
      do {
        batch = await new Promise<FileSystemEntry[]>((res, rej) => reader.readEntries(res, rej));
        out.push(...(await readEntries(batch)));
      } while (batch.length);
    }
  }
  return out.filter((f) => !f.name.startsWith("."));
}
