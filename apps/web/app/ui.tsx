import type { Project } from "@/lib/api";

export const LENGTHS = [30, 60, 120, 180, 360, 600];

const STATUS: Record<string, [string, string]> = {
  empty: ["Add clips", ""],
  uploading: ["Checking clips", "accent"],
  ready: ["Ready to cut", "accent"],
  analyzing: ["Watching clips", "accent"],
  planning: ["Planning", "accent"],
  rendering: ["Rendering", "accent"],
  ready_to_edit: ["Rough cut ready", "good"],
  exporting: ["Making HD video", "accent"],
  completed: ["HD video ready", "good"],
  needs_attention: ["Needs attention", "bad"],
};

export function StatusPill({ project }: { project: Project }) {
  const [label, tone] = STATUS[project.status] ?? [project.status, ""];
  return <span className={`pill ${tone}`}>{label}</span>;
}

export function isWorking(p: Project | null): boolean {
  return Boolean(p?.job && !p.job.finished_at) || p?.status === "uploading";
}
