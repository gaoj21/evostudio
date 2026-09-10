import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import WorkflowRunCard from './WorkflowRunCard.jsx';
it('validates missing fields and submits edited typed values with the plan', async () => {
 const onRun = vi.fn().mockResolvedValue({ ok: true });
 render(<WorkflowRunCard proposal={{ plan_id: 'plan:1', start_at: ['clean'], inputs: {}, plan: { inputs: [{ name: 'count', type: 'int', required: true }] } }} onRun={onRun} onDismiss={() => {}} />);
 fireEvent.click(screen.getByText('Run it'));
 expect(onRun).not.toHaveBeenCalled();
 expect(screen.getByRole('alert')).toHaveTextContent('Provide count');
 fireEvent.change(screen.getByLabelText('count'), { target: { value: '4' } });
 fireEvent.click(screen.getByText('Run it'));
 await waitFor(() => expect(onRun).toHaveBeenCalledWith(expect.objectContaining({ inputs: { count: 4 }, plan_id: 'plan:1', start_at: ['clean'] })));
});
