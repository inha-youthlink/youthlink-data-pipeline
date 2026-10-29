BEGIN;

-- 모두 NULL 허용 컬럼과 새 테이블이라 채팅 테이블에 기존 행이 있어도 적용됨

-- 재시도한 질문을 같은 질문으로 알아보려고 프론트가 보낸 메시지 키를 질문에만 저장함
ALTER TABLE public.chat_message
    ADD COLUMN IF NOT EXISTS client_message_id UUID;

-- 답변 없는 질문(연결 끊김, 생성 실패)이 남을 수 있어 메시지 순서 대신 답변이 가리키는 질문으로 짝을 지음
ALTER TABLE public.chat_message
    ADD COLUMN IF NOT EXISTS reply_to_message_id BIGINT
        CONSTRAINT fk_chat_message_reply_to
        REFERENCES public.chat_message (message_id)
        ON DELETE CASCADE;

-- 질문 하나에 답변 하나는 백엔드가 지킴. 나중에 답변 다시 생성을 막지 않도록 유일 제약은 두지 않음
CREATE INDEX IF NOT EXISTS idx_chat_message_reply_to
    ON public.chat_message (reply_to_message_id)
    WHERE reply_to_message_id IS NOT NULL;

DO $migration$
BEGIN
    -- 키는 대화방 안에서만 유일하면 됨. 답변 행은 NULL이라 제약에 걸리지 않음
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.chat_message'::regclass
          AND conname = 'uq_chat_message_client_message_id'
    ) THEN
        ALTER TABLE public.chat_message
            ADD CONSTRAINT uq_chat_message_client_message_id
            UNIQUE (room_id, client_message_id);
    END IF;
END;
$migration$;

-- 지금까지는 질문과 답변을 한 트랜잭션에 저장해 답변 바로 앞 메시지가 그 질문임
WITH ordered AS (
    SELECT
        message_id,
        role,
        LAG(message_id) OVER room_order AS previous_id,
        LAG(role) OVER room_order AS previous_role
    FROM public.chat_message
    WINDOW room_order AS (PARTITION BY room_id ORDER BY message_id)
)
UPDATE public.chat_message AS answer
SET reply_to_message_id = ordered.previous_id
FROM ordered
WHERE answer.message_id = ordered.message_id
  AND ordered.role = 'assistant'
  AND ordered.previous_role = 'user'
  AND answer.reply_to_message_id IS NULL;

-- 스크랩은 대화방이 아니라 사용자(익명 X-Client-Id) 단위라 chat_room과 연결하지 않음
CREATE TABLE IF NOT EXISTS public.policy_scrap (
    user_id         VARCHAR(100) NOT NULL,
    policy_no       VARCHAR(30) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- 같은 정책을 다시 스크랩해도 한 행만 남고, 사용자별 목록 조회도 이 키로 함
    PRIMARY KEY (user_id, policy_no),

    CONSTRAINT fk_policy_scrap_policy
        FOREIGN KEY (policy_no)
        REFERENCES public.policy (policy_no)
        ON DELETE CASCADE
);

COMMIT;
