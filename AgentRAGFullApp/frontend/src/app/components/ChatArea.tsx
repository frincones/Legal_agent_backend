import { useEffect, useRef, useState } from 'react';
import ChatMessage from './ChatMessage';
import AgentThinking from './AgentThinking';
import type { ThinkingStep } from './AgentThinking';
import type { Message } from '../App';

interface ChatAreaProps {
  messages: Message[];
  isLoading?: boolean;
  thinkingSteps?: ThinkingStep[];
  onOpenActivity?: (messageId: string) => void;
  onSendClarifyAnswer?: (formattedAnswer: string) => void;
}

export function ChatArea({ messages, isLoading, thinkingSteps = [], onOpenActivity, onSendClarifyAnswer }: ChatAreaProps) {
  const endRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [userScrolled, setUserScrolled] = useState(false);

  useEffect(() => {
    if (!userScrolled) {
      endRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, thinkingSteps, isLoading, userScrolled]);

  const handleScroll = () => {
    const el = containerRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
    setUserScrolled(!atBottom);
  };

  if (messages.length === 0 && !isLoading) return null;

  return (
    <div className="flex-1 overflow-hidden">
      <div
        ref={containerRef}
        className="h-full overflow-y-auto px-3 sm:px-4 md:px-6"
        onScroll={handleScroll}
      >
        <div className="max-w-3xl mx-auto py-3 sm:py-4 md:py-6 space-y-3 sm:space-y-4">
          {messages.map((msg) => {
            // Hide the empty assistant placeholder while thinking
            // (AgentThinking component shows instead)
            if (msg.role === 'assistant' && !msg.content && isLoading) {
              return null;
            }
            return (
              <ChatMessage
                key={msg.id}
                message={msg}
                onOpenActivity={onOpenActivity}
                onSendClarifyAnswer={onSendClarifyAnswer}
              />
            );
          })}

          {/* Agent thinking timeline — replaces empty assistant placeholder */}
          {isLoading && (
            <AgentThinking
              steps={thinkingSteps}
              isThinking={true}
            />
          )}

          <div ref={endRef} />
        </div>
      </div>
    </div>
  );
}
