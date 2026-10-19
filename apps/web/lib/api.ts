export type Asset = { id: string; file: string; status: string; error: string | null };

export type Job = {
  kind: "make" | "preview" | "export";
  stage: string;
  completed: number;
  total: number;
  error: string | null;
  started_at: string;
  finished_at: string | null;
};

export type Project = {
  id: string;
  name: string;
  created_at: string;
  target_seconds: number;
  status: string;
  notes: string[];
  job: Job | null;
  current_edit_id: string | null;
  preview_edit_id: string | null;
  versions: number;
  has_preview: boolean;
  preview_is_current: boolean;
  has_export: boolean;
  storage_bytes: number;
  assets: Asset[];
};

export type Shot = {
  clip_id: string;
  kind: "main" | "cutaway";
  asset_id: string;
  file: string;
  start_s: number;
  duration_s: number;
  source_in_s: number;
  source_out_s: number;
  thumb_url: string;
  source_url: string;
  why: string;
  quote: string;
  locked: boolean;
  moment_id: string | null;
};

export type Beat = { id: string; title: string; purpose: string; duration_s: number; shots: Shot[] };

export type Storyboard = {
  edit_id: string;
  version: number;
  can_undo: boolean;
  duration_s: number;
  preview_is_current: boolean;
  beats: Beat[];
};

export class ApiError extends Error {
  constructor(public code: string, message: string, public status: number) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: init?.body && !(init.body instanceof FormData) ? { "Content-Type": "application/json" } : undefined,
  });
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = body?.error ?? {};
    const detail = Array.isArray(body?.detail) ? "Please check the values you entered." : undefined;
    throw new ApiError(err.code ?? "ERROR", err.message ?? detail ?? "Something went wrong.", res.status);
  }
  return body as T;
}

export const api = {
  projects: () => call<Project[]>("/v1/projects"),
  project: (id: string) => call<Project>(`/v1/projects/${id}`),
  create: (name: string, target_seconds: number) =>
    call<Project>("/v1/projects", { method: "POST", body: JSON.stringify({ name, target_seconds }) }),
  remove: (id: string) => call<void>(`/v1/projects/${id}`, { method: "DELETE" }),
  removeFile: (id: string, assetId: string) =>
    call<void>(`/v1/projects/${id}/files/${assetId}`, { method: "DELETE" }),
  make: (id: string, target_seconds: number) =>
    call<Project>(`/v1/projects/${id}/make`, { method: "POST", body: JSON.stringify({ target_seconds }) }),
  exportHd: (id: string) => call<Project>(`/v1/projects/${id}/export`, { method: "POST" }),
  storyboard: (id: string) => call<Storyboard>(`/v1/projects/${id}/storyboard`),
  edit: (id: string, base_edit_id: string, ops: object[]) =>
    call<Storyboard>(`/v1/projects/${id}/storyboard`, {
      method: "PATCH",
      body: JSON.stringify({ base_edit_id, ops }),
    }),
  undo: (id: string) => call<Storyboard>(`/v1/projects/${id}/undo`, { method: "POST" }),
};

/** Upload one file with progress (fetch has no upload progress, so this uses XHR). */
export function uploadFile(projectId: string, file: File, onProgress: (fraction: number) => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/v1/projects/${projectId}/files`);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => (xhr.status < 300 ? resolve() : reject(new Error("Upload failed")));
    xhr.onerror = () => reject(new Error("Upload failed. Check your connection and try again."));
    const form = new FormData();
    form.append("file", file, file.name);
    xhr.send(form);
  });
}

export function formatDuration(seconds: number): string {
  const s = Math.round(seconds);
  const m = Math.floor(s / 60);
  return m ? `${m}:${String(s % 60).padStart(2, "0")}` : `${s}s`;
}

export function formatClock(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = (seconds % 60).toFixed(1).padStart(4, "0");
  return `${m}:${s}`;
}

export function at30fps(seconds: number): object {
  // Exact source times for edit ops, at 30 fps like the project timeline.
  return { value: Math.round(seconds * 30), rate_num: 30, rate_den: 1 };
}
