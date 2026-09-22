"""Shared conversation/tool loop; pages supply context and operation adapters."""
import json
from backend.api import model_json, chat_control


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

    def ask(self):
        chat_control.check()
        answer = model_json.extract_json(self.ask_model(self.messages))
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

    def run(self, execute, initial=None, max_steps=10):
        answer = initial
        for step in range(max_steps):
            chat_control.check()
            answer = answer if answer is not None else self.ask()
            self.last_answer = answer
            operations = answer.get('operations') or []
            if not operations:
                if answer.get('reply'):
                    return answer
                self.messages.append({'role': 'user', 'content': 'Return a nonempty reply or operations to gather evidence.'})
                answer = None
                continue
            self.turns.append({'operations': operations, 'reply': answer.get('reply', '')})
            chat_control.check()
            result = execute(operations)
            chat_control.check()
            observations = result.get('observations') or []
            if result.get('stop') or not observations:
                return answer
            self.messages += [
                {'role': 'assistant', 'content': json.dumps(answer, ensure_ascii=False)},
                {'role': 'user', 'content': 'Tool observations:\n' + json.dumps(observations, ensure_ascii=False, default=str) + '\nContinue using tools if needed; otherwise reply with an empty operations list.'},
            ]
            answer = None
        return {'reply': '已达到本轮分析上限。可以继续追问或缩小范围。', 'operations': []}
