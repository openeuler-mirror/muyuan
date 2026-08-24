export type GoalPlusProgressState = "preparing" | "active" | "selecting" | "complete" | "blocked";

export interface GoalPlusCandidateProgress {
	candidateId: string;
	model?: string;
	verifierCount: number;
	bestScore?: number;
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
	sessionsStarted: number;
	verifierRunsTotal: number;
	candidates: GoalPlusCandidateProgress[];
	nextAction?: string;
}

export interface PiProgressPresentation {
	status: string;
	lines: string[];
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

function shortModelName(model: string | undefined): string {
	if (!model) return "default";
	const leaf = model.split("/").at(-1) ?? model;
	return leaf.replace(/^gpt-[0-9.]+-/, "");
}

function metricSuffix(metricName: string | undefined): string {
	return metricName ? ` ${metricName}` : "";
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

function candidateLine(candidate: GoalPlusCandidateProgress, metricName: string | undefined): string {
	return `${candidate.candidateId} · ${shortModelName(candidate.model)} · ${formatScore(candidate.bestScore)}${metricSuffix(
		metricName,
	)} · ${candidate.verifierCount} verify`;
}

export function buildPiProgressPresentation(view: GoalPlusProgressView): PiProgressPresentation {
	const best = `best ${formatScore(view.bestScore)}`;
	const statusPrefix = view.state === "complete" ? "GP complete" : view.state === "blocked" ? "GP blocked" : "GP search";
	const status = `${statusPrefix} · ${view.sessionsStarted}/${view.candidatesTotal} lanes · ${view.verifierRunsTotal} verify · ${best}`;
	const lines = [
		`Goal Plus  ${STATE_LABELS[view.state]}  ${formatDuration(view.elapsedSeconds)}`,
		`Lanes ${view.sessionsStarted}/${view.candidatesTotal} started · Verify ${view.verifierRunsTotal} · Best ${formatScore(
			view.bestScore,
		)}${metricSuffix(view.metricName)}`,
	];
	const improvement = improvementLine(view);
	if (improvement) lines.push(improvement);
	lines.push(...view.candidates.map((candidate) => candidateLine(candidate, view.metricName)));
	if (view.state === "complete") {
		const selected = view.selectedCandidateId ?? view.bestCandidateId ?? "--";
		const promotion = view.promotionPassed === true
			? "promotion passed"
			: view.promotionPassed === false
				? "promotion failed"
				: "promotion not observed";
		lines.push(`Selected ${selected} · ${promotion}`);
	} else if (view.nextAction) {
		lines.push(`Next  ${view.nextAction}`);
	}
	return { status, lines };
}

export const PI_PROGRESS_PREVIEWS: Record<"active" | "complete", GoalPlusProgressView> = {
	active: {
		state: "active",
		elapsedSeconds: 258,
		metricName: "cycles",
		metricDirection: "minimize",
		baselineScore: 147_734,
		bestCandidateId: "c001",
		bestScore: 121_806,
		candidatesTotal: 2,
		sessionsStarted: 2,
		verifierRunsTotal: 8,
		candidates: [
			{ candidateId: "c001", model: "openai-codex/gpt-5.6-terra", verifierCount: 5, bestScore: 121_806 },
			{ candidateId: "c002", model: "openai-codex/gpt-5.6-luna", verifierCount: 3, bestScore: 126_114 },
		],
		nextAction: "continue candidate loops",
	},
	complete: {
		state: "complete",
		elapsedSeconds: 582,
		metricName: "cycles",
		metricDirection: "minimize",
		baselineScore: 147_734,
		bestCandidateId: "c001",
		bestScore: 112_340,
		selectedCandidateId: "c001",
		promotionPassed: true,
		candidatesTotal: 2,
		sessionsStarted: 2,
		verifierRunsTotal: 18,
		candidates: [
			{ candidateId: "c001", model: "openai-codex/gpt-5.6-terra", verifierCount: 10, bestScore: 112_340 },
			{ candidateId: "c002", model: "openai-codex/gpt-5.6-luna", verifierCount: 8, bestScore: 118_902 },
		],
	},
};
