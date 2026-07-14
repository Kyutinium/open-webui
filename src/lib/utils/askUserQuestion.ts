// Shared AskUserQuestion helpers.
//
// The gateway surfaces AskUserQuestion / permission prompts as a Responses-API
// `function_call` item. Both the pipe (Python
// `pipelines_dev/oh_my_gateway_pipe.py::_render_ask_user_question`) and the
// frontend (when a resumed continuation itself pauses again) need to turn that
// item into a `<details type="ask_user_question">` block that
// `MarkdownTokens.svelte` dispatches to `AskUserQuestionCard`.
//
// This module is the single JS source of truth for that shape. It MUST stay in
// sync with the pipe's Python implementation — keep the JSON body fields
// (callId, name, previousResponseId, questions, raw) identical.

export type AUQOption = { label: string; description?: string };
export type AUQQuestion = { question: string; options: AUQOption[]; multiSelect: boolean };

/** Normalize a function_call `arguments` object into a uniform question list.
 *  Accepts both `{questions:[…]}` and a single `{question, options}` object,
 *  and options that are plain strings or `{label, description}`. */
export const normalizeAskQuestions = (args: any): AUQQuestion[] => {
	const rawList = Array.isArray(args?.questions) ? args.questions : [args];
	return rawList
		.filter((q: any) => q && typeof q === 'object')
		.map((q: any) => ({
			question: q.question ?? q.prompt ?? '',
			options: Array.isArray(q.options)
				? q.options
						.map((opt: any) =>
							typeof opt === 'string'
								? { label: opt, description: '' }
								: { label: opt?.label ?? '', description: opt?.description ?? '' }
						)
						.filter((opt: any) => opt.label)
				: [],
			multiSelect: !!q.multiSelect
		}));
};

/** Build the `<details type="ask_user_question">` block from a function_call
 *  item. `previousResponseId` is the `requires_action` response id the card
 *  echoes back to `/api/v1/auq/answer` so the gateway can resume the session. */
export const buildAskUserQuestionDetails = (fc: any, previousResponseId?: string): string => {
	let args: any = {};
	try {
		args = JSON.parse(fc?.arguments ?? '{}') ?? {};
	} catch {
		args = {};
	}
	const questions = normalizeAskQuestions(args);
	const name = fc?.name ?? 'AskUserQuestion';
	const body = {
		callId: fc?.call_id ?? '',
		name,
		previousResponseId: previousResponseId || undefined,
		questions,
		raw: questions.some((q) => q.question) ? undefined : args
	};
	const summary =
		name === 'AskUserQuestion' ? '❓ 추가 입력이 필요합니다' : `❓ 권한/입력 요청: ${name}`;
	return (
		`\n\n<details type="ask_user_question" done="true">\n` +
		`<summary>${summary}</summary>\n` +
		`${JSON.stringify(body)}\n` +
		`</details>\n\n`
	);
};
