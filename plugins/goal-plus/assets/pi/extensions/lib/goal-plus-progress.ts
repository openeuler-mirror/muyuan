export type GoalPlusProgressState = "preparing" | "active" | "selecting" | "complete" | "blocked";

export interface GoalPlusCandidateProgress {
	candidateId: string;
	model?: string;
	verifierCount: number;
	bestScore?: number;
	status?: string;
	lastScore?: number;
	/** How long the lane's current round has been running since its last settled verifier result. */
	roundElapsedSeconds?: number;
	/** Predicted seconds until the current round settles, from the lane's average round interval; an estimate. */
	nextResultSeconds?: number;
}

export interface GoalPlusProgressView {
	state: GoalPlusProgressState;
	elapsedSeconds: number;
	metricName?: string;
	metricDirection?: "minimize" | "maximize";
	baselineScore?: number;
	bestCandidateId?: string;
	bestScore?: number;
	selectedCandidateId?: string;
	promotionPassed?: boolean;
	candidatesTotal: number;
	lanesEvaluated: number;
	verifierRunsTotal: number;
	candidates: GoalPlusCandidateProgress[];
}

export interface PiProgressPresentation {
	lines: string[];
}

export interface GoalPlusUsageSummary {
	assistantMessages: number;
	toolCalls: number;
	processedTokens: number;
	costUsd: number;
}

export interface GoalPlusCandidateCompletion {
	candidateId: string;
	model?: string;
	score?: number;
	verifierCount: number;
	disposition?: string;
	selected: boolean;
}

export interface GoalPlusCompletionView {
	status: string;
	elapsedSeconds: number;
	presetDurationSeconds?: number;
	goalPlusId?: string;
	searchTasksTotal: number;
	candidatesTotal?: number;
	verifierRunsTotal?: number;
	selectedCandidateId?: string;
	metricName?: string;
	metricDirection?: "minimize" | "maximize";
	baselineScore?: number;
	selectedScore?: number;
	promotionPassed?: boolean;
	hostCleanupState?: string;
	reportPath?: string;
	htmlReportPath?: string;
	mainUsage: GoalPlusUsageSummary;
	workerUsage?: GoalPlusUsageSummary;
	candidates: GoalPlusCandidateCompletion[];
}

export interface PiCompletionPresentation {
	lines: string[];
	collapsedLineCount: number;
}

const STATE_LABELS: Record<GoalPlusProgressState, string> = {
	preparing: "SEARCH PREPARING",
	active: "SEARCH ACTIVE",
	selecting: "SELECTING",
	complete: "SEARCH COMPLETE",
	blocked: "SEARCH BLOCKED",
};

function formatDuration(seconds: number): string {
	const safeSeconds = Math.max(0, Math.floor(seconds));
	const hours = Math.floor(safeSeconds / 3600);
	const minutes = Math.floor((safeSeconds % 3600) / 60);
	const remainingSeconds = safeSeconds % 60;
	if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${String(remainingSeconds).padStart(2, "0")}`;
	return `${String(minutes).padStart(2, "0")}:${String(remainingSeconds).padStart(2, "0")}`;
}

function formatScore(value: number | undefined): string {
	return value === undefined ? "--" : value.toLocaleString("en-US", { maximumFractionDigits: 4 });
}

function formatHumanDuration(seconds: number): string {
	const safeSeconds = Math.max(0, Math.floor(seconds));
	const hours = Math.floor(safeSeconds / 3600);
	const minutes = Math.floor((safeSeconds % 3600) / 60);
	const remainingSeconds = safeSeconds % 60;
	const parts: string[] = [];
	if (hours > 0) parts.push(`${hours}小时`);
	if (minutes > 0) parts.push(`${minutes}分钟`);
	if (remainingSeconds > 0 || parts.length === 0) parts.push(`${remainingSeconds}秒`);
	return parts.join("");
}

function formatCompactNumber(value: number): string {
	return new Intl.NumberFormat("en-US", {
		notation: "compact",
		maximumFractionDigits: 2,
	}).format(Math.max(0, value));
}

function formatUsage(label: string, usage: GoalPlusUsageSummary): string {
	return `${label}  ${usage.assistantMessages} msgs · ${usage.toolCalls} tools · ${formatCompactNumber(
		usage.processedTokens,
	)} tokens · ~$${usage.costUsd.toFixed(4)}`;
}

function displayArtifactPath(path: string | undefined): string | undefined {
	if (!path) return undefined;
	if (path.startsWith(".gp/")) return path;
	const marker = "/.gp/";
	const markerIndex = path.indexOf(marker);
	if (markerIndex >= 0) return `.gp/${path.slice(markerIndex + marker.length)}`;
	const sessionMarker = "/session/goal-plus/";
	const sessionMarkerIndex = path.indexOf(sessionMarker);
	return sessionMarkerIndex >= 0
		? `goal-plus-session/${path.slice(sessionMarkerIndex + sessionMarker.length)}`
		: path;
}

function completionResultLine(view: GoalPlusCompletionView): string | undefined {
	if (view.selectedScore === undefined && !view.selectedCandidateId) return undefined;
	const identity = [view.selectedCandidateId, view.metricName].filter(Boolean).join(" · ");
	const details: string[] = [];
	if (view.baselineScore !== undefined && view.selectedScore !== undefined) {
		details.push(`${formatScore(view.baselineScore)} → ${formatScore(view.selectedScore)}`);
		const denominator = Math.abs(view.baselineScore);
		if (denominator > 0) {
			const direction = view.metricDirection ?? "maximize";
			const improvement = direction === "minimize"
				? (view.baselineScore - view.selectedScore) / denominator
				: (view.selectedScore - view.baselineScore) / denominator;
			details.push(`(${improvement >= 0 ? "+" : ""}${(improvement * 100).toFixed(1)}%)`);
		}
	} else if (view.selectedScore !== undefined) {
		details.push(formatScore(view.selectedScore));
	}
	return `Result  ${[identity, details.join(" ")].filter(Boolean).join(" ")}`;
}

function completionCandidateLine(candidate: GoalPlusCandidateCompletion): string {
	const parts = [
		candidate.candidateId,
		candidate.model ? shortModelName(candidate.model) : undefined,
		candidate.score === undefined ? "--" : formatScore(candidate.score),
		candidate.disposition,
		`${candidate.verifierCount} verify`,
		candidate.selected ? "selected" : undefined,
	].filter(Boolean);
	return `  ${parts.join(" · ")}`;
}

export function buildPiCompletionPresentation(view: GoalPlusCompletionView): PiCompletionPresentation {
	const elapsed = formatHumanDuration(view.elapsedSeconds);
	const lines = [view.status.toUpperCase()];
	lines.push(
		view.presetDurationSeconds === undefined
			? `Time    未设置预设时间 · 实际完成收尾时间为 ${elapsed}`
			: `Time    预设时间为 ${formatHumanDuration(view.presetDurationSeconds)} · 实际完成收尾时间为 ${elapsed}`,
	);
	const result = completionResultLine(view);
	if (result) lines.push(result);
	const searchParts: string[] = [];
	if (view.candidatesTotal !== undefined) searchParts.push(`${view.candidatesTotal} candidates`);
	if (view.verifierRunsTotal !== undefined) searchParts.push(`${view.verifierRunsTotal} verify`);
	if (view.promotionPassed === true) searchParts.push("promoted");
	if (view.promotionPassed === false) searchParts.push("promotion failed");
	if (searchParts.length > 0) lines.push(`Search  ${searchParts.join(" · ")}`);
	if (view.hostCleanupState && view.hostCleanupState !== "not_applicable") {
		lines.push(`Cleanup ${view.hostCleanupState}`);
	}

	const workerUsage = view.workerUsage ?? {
		assistantMessages: 0,
		toolCalls: 0,
		processedTokens: 0,
		costUsd: 0,
	};
	const totalUsage = {
		assistantMessages: view.mainUsage.assistantMessages + workerUsage.assistantMessages,
		toolCalls: view.mainUsage.toolCalls + workerUsage.toolCalls,
		processedTokens: view.mainUsage.processedTokens + workerUsage.processedTokens,
		costUsd: view.mainUsage.costUsd + workerUsage.costUsd,
	};
	lines.push(formatUsage("Usage ", totalUsage));
	const collapsedLineCount = lines.length;

	lines.push(formatUsage("Main  ", view.mainUsage));
	if (view.workerUsage) lines.push(formatUsage("Worker", view.workerUsage));
	if (view.candidates.length > 0) {
		lines.push("Lanes");
		lines.push(...view.candidates.map(completionCandidateLine));
	}
	const goalParts = [view.goalPlusId, `${view.searchTasksTotal} search task${view.searchTasksTotal === 1 ? "" : "s"}`]
		.filter(Boolean);
	lines.push(`Goal    ${goalParts.join(" · ")}`);
	const reportPath = displayArtifactPath(view.reportPath);
	const htmlReportPath = displayArtifactPath(view.htmlReportPath);
	if (reportPath) lines.push(`Report  ${reportPath}`);
	if (htmlReportPath) lines.push(`HTML    ${htmlReportPath}`);
	return { lines, collapsedLineCount };
}

function shortModelName(model: string | undefined): string {
	if (!model) return "default";
	const leaf = model.split("/").at(-1) ?? model;
	return leaf.replace(/^gpt-[0-9.]+-/, "");
}

function improvementLine(view: GoalPlusProgressView): string | undefined {
	if (view.baselineScore === undefined || view.bestScore === undefined) return undefined;
	const denominator = Math.abs(view.baselineScore);
	if (denominator === 0) return undefined;
	const direction = view.metricDirection ?? "maximize";
	const improvement = direction === "minimize"
		? (view.baselineScore - view.bestScore) / denominator
		: (view.bestScore - view.baselineScore) / denominator;
	const change = improvement >= 0
		? `${(improvement * 100).toFixed(1)}% better`
		: `${Math.abs(improvement * 100).toFixed(1)}% worse`;
	return `Baseline ${formatScore(view.baselineScore)} → Best ${formatScore(view.bestScore)} · ${change}`;
}

function candidateLine(candidate: GoalPlusCandidateProgress): string {
	const parts = [
		candidate.candidateId,
		candidate.model ? shortModelName(candidate.model) : undefined,
		candidate.status,
	];
	if (candidate.lastScore !== undefined && candidate.bestScore !== undefined) {
		parts.push(`best ${formatScore(candidate.bestScore)}`, `last ${formatScore(candidate.lastScore)}`);
	} else {
		parts.push(formatScore(candidate.bestScore));
	}
	if (candidate.roundElapsedSeconds !== undefined) {
		parts.push(`round ${formatDuration(candidate.roundElapsedSeconds)}`);
		if (candidate.nextResultSeconds !== undefined) {
			parts.push(`next ~${formatDuration(candidate.nextResultSeconds)}`);
		}
	}
	parts.push(`${candidate.verifierCount} verify`);
	return parts.filter(Boolean).join(" · ");
}

export function buildPiProgressPresentation(view: GoalPlusProgressView): PiProgressPresentation {
	const header = `Goal Plus  ${STATE_LABELS[view.state]}  ${formatDuration(view.elapsedSeconds)}`;
	const lines = [
		view.metricName ? `${header} · ${view.metricName}` : header,
		`Lanes ${view.lanesEvaluated}/${view.candidatesTotal} evaluated · Verify ${view.verifierRunsTotal} · Best ${formatScore(
			view.bestScore,
		)}`,
	];
	const improvement = improvementLine(view);
	if (improvement) lines.push(improvement);
	lines.push(...view.candidates.map(candidateLine));
	if (view.state === "complete") {
		const selected = view.selectedCandidateId ?? view.bestCandidateId ?? "--";
		const promotion = view.promotionPassed === true
			? "promotion passed"
			: view.promotionPassed === false
				? "promotion failed"
				: "promotion not observed";
		lines.push(`Selected ${selected} · ${promotion}`);
	}
	return { lines };
}
