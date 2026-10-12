// External contracts audited against static/index.html, app-core.js and Python routers.
// Required IDs are present in the ordered classic-script page; dynamic IDs stay nullable.
interface HTMLElement { _reportListRequest?: object }
interface Document {
 getElementById(id: 'btn-report-explain' | 'btn-report-explanation-cancel' | 'btn-benchmark-start' | 'btn-benchmark-cancel'): HTMLButtonElement;
 getElementById(id: 'benchmark-manifest'): HTMLTextAreaElement;
 getElementById(id: 'report-view-title' | 'report-view-content' | 'benchmark-output'): HTMLElement;
 querySelectorAll(selectors: '#reports-list .report-list-item'): NodeListOf<HTMLElement>;
}
type RunStatus = 'running' | 'completed' | 'failed' | 'interrupted' | 'cancelled';
interface ReportRow { filename: string; can_explain: boolean; mtime: number; type: string }
interface Checkpoint {book:string; total_batches:number; completed_batches:number; failed_batches?: number[]; batches_skipped_vram?:number; entries_done:number; resume_from_batch:number; mtime:number}
interface Checkpoints {checkpoints:Checkpoint[]; live: null | {bidirectional:boolean; current_pass:string|null; tasks:{name:string|null; status:string|null}[]}}
interface ExplanationStatus {run_id:string|null; running:boolean; status:'idle'|'pending'|'running'|'done'|'failed'|'cancelled'; error:string|null}
interface BenchmarkStatus {running:boolean; status:string; tasks?:{status:string}[]; logs?:string[]}
interface RunRow {started_at:string|null; finished_at:string|null; task:string; status:RunStatus; error:string|null}
type Manifest = Record<string, unknown>;
interface APIContract {
 get(path:'/api/reports'): Promise<ReportRow[]>;
 get(path:'/api/review/checkpoints'): Promise<Checkpoints>;
 get(path:'/api/status/report_explanation'): Promise<ExplanationStatus>;
 get(path:'/api/status/version'): Promise<{revision:string|null;short_revision:string|null;branch:string|null;revision_source:string}>;
 get(path:'/api/runs?limit=50'):Promise<{runs:RunRow[]}>;
 get(path:'/api/benchmark/status'):Promise<BenchmarkStatus>;
 post(path:`/api/reports/${string}/explain`, body:Record<string,never>):Promise<{run_id:string;status:'started';filename:string}>;
 post(path:'/api/reports/explanation/cancel',body:{run_id:string}):Promise<{status:'cancel_requested'}>;
 post(path:'/api/benchmark/preflight',body:{manifest:Manifest}):Promise<{preflight_id:string}>;
 post(path:'/api/benchmark/start',body:{manifest:Manifest;preflight_id:string}):Promise<unknown>;
 post(path:'/api/benchmark/cancel',body:Record<string,never>):Promise<unknown>;
}
declare const API:APIContract;
declare const marked:{parse(markdown:string):string};
declare const DOMPurify:{sanitize(html:string):string};
declare function escapeHtml(text:string):string;
declare function getActionErrorMessage(label:string,error:unknown,help:string):string;
declare function showActionError(label:string,error:unknown,help:string):void;
declare function confirmIfRemote(label:string):Promise<boolean>;
declare function showToast(text:string,kind:'warning'|'success'):void;
declare function restoreTab():void;
declare function _startPolling<T>(key:string, fetchFn:()=>Promise<T>,options:{intervalMs?:number;immediate?:boolean;pauseWhenHidden?:boolean;displayLabel?:string|null;doneCheck:(data:T)=>boolean;onTick?:(data:T)=>unknown;onDone?:(data:T)=>unknown}):()=>void;
