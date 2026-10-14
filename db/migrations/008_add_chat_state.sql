BEGIN;

-- user_id, display_order는 기본값 없는 NOT NULL이라 채팅 테이블이 비어 있다는 전제로 적용함
-- 기존 행이 있으면 실패하며, 이때 처리 방법은 PR 설명을 따름

-- Spring이 넘기는 사용자 식별값. RAG DB에 사용자 테이블이 없어 FK를 두지 않음
ALTER TABLE public.chat_room
    ADD COLUMN IF NOT EXISTS user_id VARCHAR(100) NOT NULL;

-- 사용자별 채팅방 목록을 최근 대화 순으로 조회하기 위한 인덱스
CREATE INDEX IF NOT EXISTS idx_chat_room_user_updated
    ON public.chat_room (user_id, updated_at DESC);

-- 질문 분석 결과처럼 형태가 자주 바뀌는 정보는 컬럼 대신 JSON으로 둠
ALTER TABLE public.chat_message
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

-- "두 번째 거"는 점수 순서가 아니라 사용자에게 보여준 순서 기준이라 별도 컬럼으로 둠
ALTER TABLE public.message_policy_ref
    ADD COLUMN IF NOT EXISTS display_order SMALLINT NOT NULL;

-- 한 질문에서 여러 정책을 다룰 수 있어 현재 정책을 목록으로 둠
CREATE TABLE IF NOT EXISTS public.chat_current_policy (
    room_id         UUID NOT NULL,
    policy_no       VARCHAR(30) NOT NULL,
    display_order   SMALLINT NOT NULL,

    PRIMARY KEY (room_id, policy_no),

    CONSTRAINT fk_chat_current_policy_room
        FOREIGN KEY (room_id)
        REFERENCES public.chat_room (room_id)
        ON DELETE CASCADE,

    CONSTRAINT fk_chat_current_policy_policy
        FOREIGN KEY (policy_no)
        REFERENCES public.policy (policy_no)
        ON DELETE CASCADE,

    CONSTRAINT uq_chat_current_policy_order
        UNIQUE (room_id, display_order),

    CONSTRAINT chk_chat_current_policy_order
        CHECK (display_order >= 1)
);

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.message_policy_ref'::regclass
          AND conname = 'uq_message_policy_ref_order'
    ) THEN
        ALTER TABLE public.message_policy_ref
            ADD CONSTRAINT uq_message_policy_ref_order
            UNIQUE (message_id, display_order);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.message_policy_ref'::regclass
          AND conname = 'chk_message_policy_ref_order'
    ) THEN
        ALTER TABLE public.message_policy_ref
            ADD CONSTRAINT chk_message_policy_ref_order
            CHECK (display_order >= 1);
    END IF;
END;
$migration$;

COMMIT;
