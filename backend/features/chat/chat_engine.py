"""Shared conversation/tool loop; pages supply context and operation adapters."""
import json
import re
from backend.api import model_json, chat_control

# A reply saying a change was made. Checked against what the turn actually
# did: "已添加" with nothing behind it is reminded, whatever the request said.
_CLAIM = re.compile(
    r"已(经)?(成功)?(添加|增加|新增|加上|加入|创建|新建|建好|接上|接入|连接|连上|删除|删掉|移除|更新|修改|改好|重命名|设置|配置)"
    r"|\b(i(?:'ve| have)?|has been|have been|was|were)\s+(?:now\s+)?"
    r"(added|created|connected|attached|deleted|removed|updated|renamed|configured)\b",
    re.I)


def claims_change(reply) -> bool:
    return bool(_CLAIM.search(str(reply or "")))


class ChatEngine:
    def __init__(self, ask, prompt, history, message, context=None):
        self.ask_model = ask
        self.messages = [{'role': 'system', 'content': prompt}]
        if context is not None:
            self.messages.append({'role': 'system', 'content': json.dumps(context, ensure_ascii=False, default=str)})
        self.messages += [{'role': m['role'], 'content': str(m.get('content') or '')[:12000]}
                          for m in (history or [])[-12:] if isinstance(m, dict) and m.get('role') in ('user', 'assistant')]
        self.messages.append({'role': 'user', 'content': message})
        self.last_answer = None
        self.turns = []
        # Did any step of this turn change something (canvas, Agents, files)?
        self.changed = False

    def ask(self):
        chat_control.check()
        text = self.ask_model(self.messages)
        chat_control.check()
        try:
            answer = model_json.extract_json(text)
        except model_json.MalformedJSON:
            # A cut-off reply is asked for again, whole; none of it is acted on.
            retry = self.messages + [
                {'role': 'assistant', 'content': str(text)[:4000]},
                {'role': 'user', 'content': 'Your reply was cut off or was not valid JSON, so none of it was applied. '
                                            'Send the complete JSON object again; keep it shorter if it was long.'}]
            try:
                answer = model_json.extract_json(self.ask_model(retry))
            except model_json.MalformedJSON:
                raise ValueError('The model reply was cut off or malformed, so nothing was applied. Please retry.')
            chat_control.check()
        if not isinstance(answer, dict):
            raise ValueError('The model returned an invalid response. Please retry.')
        if answer.get('op'):
            answer = {'operations': [answer]}
        operations = answer.get('operations') or []
        if not isinstance(operations, list):
            raise ValueError('The model returned malformed operations. Please retry.')
        self.last_answer = {**answer, 'reply': str(answer.get('reply') or answer.get('message') or ''), 'operations': operations}
        return self.last_answer

    def run(self, execute, initial=None, max_steps=10, expects_edit=False):
        """Ask, execute, observe, repeat.

        `expects_edit`: the user asked for a change. A first answer that
        claims one without any operation is reminded once that nothing was
        applied, so "I deleted it" cannot stand in for the deletion.
        """
        answer = initial
        reminded = False
        for step in range(max_steps):
            chat_control.check()
            answer = answer if answer is not None else self.ask()
            self.last_answer = answer
            operations = answer.get('operations') or []
            if not operations:
                unbacked = (expects_edit and not self.turns) or (
                    claims_change(answer.get('reply')) and not self.changed)
                if answer.get('reply') and unbacked and not reminded:
                    reminded = True
                    self.messages += [
                        {'role': 'assistant', 'content': json.dumps(answer, ensure_ascii=False)},
                        {'role': 'user', 'content': 'Your reply has no operations, so nothing was changed. '
                                                    'If the user asked for a change, return the operations that make it now. '
                                                    'If you cannot (for example nothing on the canvas matches), say so plainly '
                                                    'and do not claim the change was made.'},
                    ]
                    answer = None
                    continue
                if answer.get('reply'):
                    return answer
                self.messages.append({'role': 'user', 'content': 'Return a nonempty reply or operations to gather evidence.'})
                answer = None
                continue
            self.turns.append({'operations': operations, 'reply': answer.get('reply', '')})
            chat_control.check()
            result = execute(operations)
            chat_control.check()
            self.changed = self.changed or bool(result.get('changed'))
            observations = result.get('observations') or []
            if result.get('stop') or not observations:
                return answer
            # The canvas summary in the system prompt is from the start of
            # the turn; the page restates what it looks like now.
            canvas = ('\nCurrent canvas after these operations (authoritative):\n' + result['canvas']) if result.get('canvas') else ''
            self.messages += [
                {'role': 'assistant', 'content': json.dumps(answer, ensure_ascii=False)},
                {'role': 'user', 'content': 'Tool observations:\n' + json.dumps(observations, ensure_ascii=False, default=str) + canvas + '\nContinue using tools if needed; otherwise reply with an empty operations list.'},
            ]
            answer = None
        return {'reply': 'Reached the step limit for this turn. Ask a follow-up or narrow the request.', 'operations': []}
