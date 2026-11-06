BEGIN;

SET LOCAL search_path = pg_catalog, public;

-- 008/009 번호가 정책·웹 마이그레이션과 중복되어 일부 환경에서 chat 변경이
-- 누락되었다. 기존 파일 이름은 공유된 적용 이력을 위해 유지하고, 이 파일에서
-- 미적용·부분 적용·완전 적용 상태를 db/schema.sql의 최종 chat 구조로 수렴시킨다.
--
-- user_id와 display_order는 기존 데이터에서 신뢰할 수 있게 추론할 수 없다.
-- 필요한 컬럼이 없는 상태로 레거시 행이 있거나 NULL이 남아 있으면 임의의 값을
-- 만들지 않고 전체 트랜잭션을 중단한다. 운영자는 검증된 매핑으로 nullable 컬럼을
-- 먼저 채운 뒤 이 마이그레이션을 다시 실행해야 한다.

DO $migration$
DECLARE
    relation_name TEXT;
    has_rows BOOLEAN;
    has_null BOOLEAN;
BEGIN
    FOREACH relation_name IN ARRAY ARRAY[
        'public.policy',
        'public.chat_room',
        'public.chat_message',
        'public.message_policy_ref'
    ]
    LOOP
        IF to_regclass(relation_name) IS NULL THEN
            RAISE EXCEPTION
                '015_converge_chat_schema requires base table %', relation_name;
        END IF;
    END LOOP;

    SELECT EXISTS (SELECT 1 FROM public.chat_room) INTO has_rows;
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'chat_room'
          AND column_name = 'user_id'
    ) THEN
        IF has_rows THEN
            RAISE EXCEPTION USING
                MESSAGE = 'cannot add chat_room.user_id safely: legacy chat_room rows exist',
                HINT = 'Add user_id as nullable, populate it from a verified user-to-room mapping, then rerun migration 015.';
        END IF;
    ELSE
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.chat_room WHERE user_id IS NULL)'
            INTO has_null;
        IF has_null THEN
            RAISE EXCEPTION USING
                MESSAGE = 'cannot enforce chat_room.user_id: NULL legacy values exist',
                HINT = 'Populate every NULL user_id from a verified user-to-room mapping, then rerun migration 015.';
        END IF;
    END IF;

    SELECT EXISTS (SELECT 1 FROM public.message_policy_ref) INTO has_rows;
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'message_policy_ref'
          AND column_name = 'display_order'
    ) THEN
        IF has_rows THEN
            RAISE EXCEPTION USING
                MESSAGE = 'cannot add message_policy_ref.display_order safely: legacy reference rows exist',
                HINT = 'Add display_order as nullable, populate the verified user-visible order for each message, then rerun migration 015.';
        END IF;
    ELSE
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.message_policy_ref WHERE display_order IS NULL)'
            INTO has_null;
        IF has_null THEN
            RAISE EXCEPTION USING
                MESSAGE = 'cannot enforce message_policy_ref.display_order: NULL legacy values exist',
                HINT = 'Populate the verified user-visible order for every NULL row, then rerun migration 015.';
        END IF;
    END IF;

    IF to_regclass('public.chat_current_policy') IS NOT NULL THEN
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.chat_current_policy)' INTO has_rows;
        IF has_rows THEN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'chat_current_policy'
                  AND column_name = 'room_id'
            ) OR NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'chat_current_policy'
                  AND column_name = 'policy_no'
            ) OR NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'chat_current_policy'
                  AND column_name = 'display_order'
            ) THEN
                RAISE EXCEPTION USING
                    MESSAGE = 'cannot repair populated chat_current_policy with missing required columns',
                    HINT = 'Restore room_id, policy_no, and display_order from verified source data, then rerun migration 015.';
            END IF;

            EXECUTE 'SELECT EXISTS (
                SELECT 1 FROM public.chat_current_policy
                WHERE room_id IS NULL OR policy_no IS NULL OR display_order IS NULL
            )' INTO has_null;
            IF has_null THEN
                RAISE EXCEPTION USING
                    MESSAGE = 'cannot enforce chat_current_policy required columns: NULL legacy values exist',
                    HINT = 'Populate required values from verified source data, then rerun migration 015.';
            END IF;
        END IF;
    END IF;

    IF to_regclass('public.policy_scrap') IS NOT NULL THEN
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.policy_scrap)' INTO has_rows;
        IF has_rows THEN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'policy_scrap'
                  AND column_name = 'user_id'
            ) OR NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'policy_scrap'
                  AND column_name = 'policy_no'
            ) THEN
                RAISE EXCEPTION USING
                    MESSAGE = 'cannot repair populated policy_scrap with missing identity columns',
                    HINT = 'Restore user_id and policy_no from verified source data, then rerun migration 015.';
            END IF;

            EXECUTE 'SELECT EXISTS (
                SELECT 1 FROM public.policy_scrap
                WHERE user_id IS NULL OR policy_no IS NULL
            )' INTO has_null;
            IF has_null THEN
                RAISE EXCEPTION USING
                    MESSAGE = 'cannot enforce policy_scrap identity columns: NULL legacy values exist',
                    HINT = 'Populate required values from verified source data, then rerun migration 015.';
            END IF;
        END IF;
    END IF;
END;
$migration$;

ALTER TABLE public.chat_room
    ADD COLUMN IF NOT EXISTS user_id VARCHAR(100);

ALTER TABLE public.chat_message
    ADD COLUMN IF NOT EXISTS metadata JSONB,
    ADD COLUMN IF NOT EXISTS client_message_id UUID,
    ADD COLUMN IF NOT EXISTS reply_to_message_id BIGINT;

ALTER TABLE public.message_policy_ref
    ADD COLUMN IF NOT EXISTS display_order SMALLINT;

CREATE TABLE IF NOT EXISTS public.chat_current_policy (
    room_id         UUID NOT NULL,
    policy_no       VARCHAR(30) NOT NULL,
    display_order   SMALLINT NOT NULL,

    CONSTRAINT chat_current_policy_pkey
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

ALTER TABLE public.chat_current_policy
    ADD COLUMN IF NOT EXISTS room_id UUID,
    ADD COLUMN IF NOT EXISTS policy_no VARCHAR(30),
    ADD COLUMN IF NOT EXISTS display_order SMALLINT;

CREATE TABLE IF NOT EXISTS public.policy_scrap (
    user_id         VARCHAR(100) NOT NULL,
    policy_no       VARCHAR(30) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT policy_scrap_pkey
        PRIMARY KEY (user_id, policy_no),
    CONSTRAINT fk_policy_scrap_policy
        FOREIGN KEY (policy_no)
        REFERENCES public.policy (policy_no)
        ON DELETE CASCADE
);

ALTER TABLE public.policy_scrap
    ADD COLUMN IF NOT EXISTS user_id VARCHAR(100),
    ADD COLUMN IF NOT EXISTS policy_no VARCHAR(30),
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;

-- IF NOT EXISTS는 이미 존재하는 컬럼의 잘못된 타입을 교정하지 않는다. 임의의
-- 타입 변환으로 데이터를 손상시키지 않고, 기대 타입이 아니면 명확히 중단한다.
DO $migration$
DECLARE
    mismatch RECORD;
BEGIN
    FOR mismatch IN
        WITH expected(table_name, column_name, expected_type) AS (
            VALUES
                ('chat_room', 'user_id', 'character varying(100)'),
                ('chat_message', 'metadata', 'jsonb'),
                ('chat_message', 'client_message_id', 'uuid'),
                ('chat_message', 'reply_to_message_id', 'bigint'),
                ('message_policy_ref', 'display_order', 'smallint'),
                ('chat_current_policy', 'room_id', 'uuid'),
                ('chat_current_policy', 'policy_no', 'character varying(30)'),
                ('chat_current_policy', 'display_order', 'smallint'),
                ('policy_scrap', 'user_id', 'character varying(100)'),
                ('policy_scrap', 'policy_no', 'character varying(30)'),
                ('policy_scrap', 'created_at', 'timestamp with time zone')
        )
        SELECT
            expected.table_name,
            expected.column_name,
            expected.expected_type,
            COALESCE(format_type(attribute.atttypid, attribute.atttypmod), '<missing>') AS actual_type
        FROM expected
        LEFT JOIN pg_namespace AS namespace
            ON namespace.nspname = 'public'
        LEFT JOIN pg_class AS relation
            ON relation.relnamespace = namespace.oid
           AND relation.relname = expected.table_name
        LEFT JOIN pg_attribute AS attribute
            ON attribute.attrelid = relation.oid
           AND attribute.attname = expected.column_name
           AND attribute.attnum > 0
           AND NOT attribute.attisdropped
        WHERE attribute.attnum IS NULL
           OR format_type(attribute.atttypid, attribute.atttypmod) <> expected.expected_type
    LOOP
        RAISE EXCEPTION
            'unexpected type for %.%: expected %, found %',
            mismatch.table_name,
            mismatch.column_name,
            mismatch.expected_type,
            mismatch.actual_type;
    END LOOP;
END;
$migration$;

UPDATE public.chat_message
SET metadata = '{}'::jsonb
WHERE metadata IS NULL;

UPDATE public.policy_scrap
SET created_at = NOW()
WHERE created_at IS NULL;

-- 009의 보수적 레거시 보정과 동일하게, 같은 방에서 assistant 바로 앞 행이
-- user인 경우만 연결한다. system/tool 메시지가 끼거나 순서가 불명확하면 추론하지 않는다.
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

DO $migration$
BEGIN
    IF EXISTS (SELECT 1 FROM public.chat_room WHERE user_id IS NULL) THEN
        RAISE EXCEPTION 'chat_room.user_id still contains NULL values';
    END IF;

    IF EXISTS (SELECT 1 FROM public.message_policy_ref WHERE display_order IS NULL) THEN
        RAISE EXCEPTION 'message_policy_ref.display_order still contains NULL values';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.message_policy_ref
        WHERE display_order < 1
    ) THEN
        RAISE EXCEPTION 'message_policy_ref.display_order must be at least 1';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.message_policy_ref
        GROUP BY message_id, display_order
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION 'duplicate message_policy_ref (message_id, display_order) values exist';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.chat_message
        WHERE client_message_id IS NOT NULL
        GROUP BY room_id, client_message_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION 'duplicate chat_message (room_id, client_message_id) values exist';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.chat_message AS answer
        LEFT JOIN public.chat_message AS question
            ON question.message_id = answer.reply_to_message_id
        WHERE answer.reply_to_message_id IS NOT NULL
          AND question.message_id IS NULL
    ) THEN
        RAISE EXCEPTION 'orphan chat_message.reply_to_message_id values exist';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.chat_current_policy
        WHERE room_id IS NULL OR policy_no IS NULL OR display_order IS NULL
    ) THEN
        RAISE EXCEPTION 'chat_current_policy contains NULL required values';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.chat_current_policy
        WHERE display_order < 1
    ) THEN
        RAISE EXCEPTION 'chat_current_policy.display_order must be at least 1';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.chat_current_policy
        GROUP BY room_id, policy_no
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION 'duplicate chat_current_policy (room_id, policy_no) values exist';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.chat_current_policy
        GROUP BY room_id, display_order
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION 'duplicate chat_current_policy (room_id, display_order) values exist';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.chat_current_policy AS current_policy
        LEFT JOIN public.chat_room AS room
            ON room.room_id = current_policy.room_id
        WHERE room.room_id IS NULL
    ) THEN
        RAISE EXCEPTION 'orphan chat_current_policy.room_id values exist';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.chat_current_policy AS current_policy
        LEFT JOIN public.policy AS policy_row
            ON policy_row.policy_no = current_policy.policy_no
        WHERE policy_row.policy_no IS NULL
    ) THEN
        RAISE EXCEPTION 'orphan chat_current_policy.policy_no values exist';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.policy_scrap
        WHERE user_id IS NULL OR policy_no IS NULL OR created_at IS NULL
    ) THEN
        RAISE EXCEPTION 'policy_scrap contains NULL required values';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.policy_scrap
        GROUP BY user_id, policy_no
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION 'duplicate policy_scrap (user_id, policy_no) values exist';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.policy_scrap AS scrap
        LEFT JOIN public.policy AS policy_row
            ON policy_row.policy_no = scrap.policy_no
        WHERE policy_row.policy_no IS NULL
    ) THEN
        RAISE EXCEPTION 'orphan policy_scrap.policy_no values exist';
    END IF;
END;
$migration$;

ALTER TABLE public.chat_room
    ALTER COLUMN user_id DROP DEFAULT,
    ALTER COLUMN user_id SET NOT NULL;

ALTER TABLE public.chat_message
    ALTER COLUMN metadata SET DEFAULT '{}'::jsonb,
    ALTER COLUMN metadata SET NOT NULL,
    ALTER COLUMN client_message_id DROP DEFAULT,
    ALTER COLUMN client_message_id DROP NOT NULL,
    ALTER COLUMN reply_to_message_id DROP DEFAULT,
    ALTER COLUMN reply_to_message_id DROP NOT NULL;

ALTER TABLE public.message_policy_ref
    ALTER COLUMN display_order DROP DEFAULT,
    ALTER COLUMN display_order SET NOT NULL;

ALTER TABLE public.chat_current_policy
    ALTER COLUMN room_id DROP DEFAULT,
    ALTER COLUMN room_id SET NOT NULL,
    ALTER COLUMN policy_no DROP DEFAULT,
    ALTER COLUMN policy_no SET NOT NULL,
    ALTER COLUMN display_order DROP DEFAULT,
    ALTER COLUMN display_order SET NOT NULL;

ALTER TABLE public.policy_scrap
    ALTER COLUMN user_id DROP DEFAULT,
    ALTER COLUMN user_id SET NOT NULL,
    ALTER COLUMN policy_no DROP DEFAULT,
    ALTER COLUMN policy_no SET NOT NULL,
    ALTER COLUMN created_at SET DEFAULT NOW(),
    ALTER COLUMN created_at SET NOT NULL;

-- 동명 제약이 있다는 사실만으로는 최종 구조를 보장할 수 없다. PostgreSQL이
-- 역직렬화한 정의를 비교해 컬럼 순서, 참조 대상, 삭제 동작, CHECK 식까지 모두
-- 일치하는 경우만 기존 제약을 재사용한다. NOT VALID만 다른 제약은 아래에서 검증한다.
DO $migration$
DECLARE
    mismatch RECORD;
BEGIN
    FOR mismatch IN
        WITH expected(relation_id, constraint_name, expected_definition) AS (
            VALUES
                (
                    'public.chat_message'::regclass,
                    'fk_chat_message_reply_to',
                    'FOREIGN KEY (reply_to_message_id) REFERENCES chat_message(message_id) ON DELETE CASCADE'
                ),
                (
                    'public.chat_message'::regclass,
                    'uq_chat_message_client_message_id',
                    'UNIQUE (room_id, client_message_id)'
                ),
                (
                    'public.message_policy_ref'::regclass,
                    'uq_message_policy_ref_order',
                    'UNIQUE (message_id, display_order)'
                ),
                (
                    'public.message_policy_ref'::regclass,
                    'chk_message_policy_ref_order',
                    'CHECK (display_order >= 1)'
                ),
                (
                    'public.chat_current_policy'::regclass,
                    'chat_current_policy_pkey',
                    'PRIMARY KEY (room_id, policy_no)'
                ),
                (
                    'public.chat_current_policy'::regclass,
                    'fk_chat_current_policy_room',
                    'FOREIGN KEY (room_id) REFERENCES chat_room(room_id) ON DELETE CASCADE'
                ),
                (
                    'public.chat_current_policy'::regclass,
                    'fk_chat_current_policy_policy',
                    'FOREIGN KEY (policy_no) REFERENCES policy(policy_no) ON DELETE CASCADE'
                ),
                (
                    'public.chat_current_policy'::regclass,
                    'uq_chat_current_policy_order',
                    'UNIQUE (room_id, display_order)'
                ),
                (
                    'public.chat_current_policy'::regclass,
                    'chk_chat_current_policy_order',
                    'CHECK (display_order >= 1)'
                ),
                (
                    'public.policy_scrap'::regclass,
                    'policy_scrap_pkey',
                    'PRIMARY KEY (user_id, policy_no)'
                ),
                (
                    'public.policy_scrap'::regclass,
                    'fk_policy_scrap_policy',
                    'FOREIGN KEY (policy_no) REFERENCES policy(policy_no) ON DELETE CASCADE'
                )
        )
        SELECT
            expected.relation_id::regclass::TEXT AS relation_name,
            expected.constraint_name,
            expected.expected_definition,
            pg_get_constraintdef(constraint_row.oid, TRUE) AS actual_definition
        FROM expected
        JOIN pg_constraint AS constraint_row
          ON constraint_row.conrelid = expected.relation_id
         AND constraint_row.conname = expected.constraint_name
        WHERE regexp_replace(
                  pg_get_constraintdef(constraint_row.oid, TRUE),
                  ' NOT VALID$',
                  ''
              ) <> expected.expected_definition
    LOOP
        RAISE EXCEPTION USING
            MESSAGE = format(
                'constraint %s.%I has an unexpected definition',
                mismatch.relation_name,
                mismatch.constraint_name
            ),
            DETAIL = format(
                'expected: %s; found: %s',
                mismatch.expected_definition,
                mismatch.actual_definition
            );
    END LOOP;
END;
$migration$;

-- 올바른 정의지만 NOT VALID인 부분 적용 FK/CHECK는 기존 데이터 검사를 마친 뒤
-- 정상 제약으로 수렴시킨다.
DO $migration$
DECLARE
    constraint_row RECORD;
BEGIN
    FOR constraint_row IN
        SELECT constraint_catalog.conrelid, constraint_catalog.conname
        FROM pg_constraint AS constraint_catalog
        WHERE NOT constraint_catalog.convalidated
          AND (
              (constraint_catalog.conrelid = 'public.chat_message'::regclass
               AND constraint_catalog.conname = 'fk_chat_message_reply_to')
              OR (constraint_catalog.conrelid = 'public.message_policy_ref'::regclass
                  AND constraint_catalog.conname = 'chk_message_policy_ref_order')
              OR (constraint_catalog.conrelid = 'public.chat_current_policy'::regclass
                  AND constraint_catalog.conname IN (
                      'fk_chat_current_policy_room',
                      'fk_chat_current_policy_policy',
                      'chk_chat_current_policy_order'
                  ))
              OR (constraint_catalog.conrelid = 'public.policy_scrap'::regclass
                  AND constraint_catalog.conname = 'fk_policy_scrap_policy')
          )
    LOOP
        EXECUTE format(
            'ALTER TABLE %s VALIDATE CONSTRAINT %I',
            constraint_row.conrelid::regclass,
            constraint_row.conname
        );
    END LOOP;
END;
$migration$;

-- 기대 이름이 없고 다른 이름의 제약이 있는 부분 적용 상태에서도, 전체 정의와
-- validation·deferrability 속성이 정확한 제약만 재사용한다.
DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_message'::regclass
          AND conname = 'fk_chat_message_reply_to'
          AND contype <> 'f'
    ) THEN
        RAISE EXCEPTION 'constraint fk_chat_message_reply_to exists with unexpected type';
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_message'::regclass
          AND contype = 'f'
          AND conkey = ARRAY[(
              SELECT attnum FROM pg_attribute
              WHERE attrelid = 'public.chat_message'::regclass
                AND attname = 'reply_to_message_id'
          )]::SMALLINT[]
          AND confrelid = 'public.chat_message'::regclass
          AND confkey = ARRAY[(
              SELECT attnum FROM pg_attribute
              WHERE attrelid = 'public.chat_message'::regclass
                AND attname = 'message_id'
          )]::SMALLINT[]
          AND confdeltype = 'c'
          AND convalidated
          AND NOT condeferrable
          AND NOT condeferred
          AND confupdtype = 'a'
          AND confmatchtype = 's'
          AND pg_get_constraintdef(oid, TRUE)
              = 'FOREIGN KEY (reply_to_message_id) REFERENCES chat_message(message_id) ON DELETE CASCADE'
    ) THEN
        ALTER TABLE public.chat_message
            ADD CONSTRAINT fk_chat_message_reply_to
            FOREIGN KEY (reply_to_message_id)
            REFERENCES public.chat_message (message_id)
            ON DELETE CASCADE;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_message'::regclass
          AND conname = 'uq_chat_message_client_message_id'
          AND contype <> 'u'
    ) THEN
        RAISE EXCEPTION 'constraint uq_chat_message_client_message_id exists with unexpected type';
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_message'::regclass
          AND contype = 'u'
          AND conkey = ARRAY[
              (SELECT attnum FROM pg_attribute WHERE attrelid = 'public.chat_message'::regclass AND attname = 'room_id'),
              (SELECT attnum FROM pg_attribute WHERE attrelid = 'public.chat_message'::regclass AND attname = 'client_message_id')
          ]::SMALLINT[]
          AND convalidated
          AND NOT condeferrable
          AND NOT condeferred
          AND pg_get_constraintdef(oid, TRUE)
              = 'UNIQUE (room_id, client_message_id)'
    ) THEN
        ALTER TABLE public.chat_message
            ADD CONSTRAINT uq_chat_message_client_message_id
            UNIQUE (room_id, client_message_id);
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.message_policy_ref'::regclass
          AND conname = 'uq_message_policy_ref_order'
          AND contype <> 'u'
    ) THEN
        RAISE EXCEPTION 'constraint uq_message_policy_ref_order exists with unexpected type';
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.message_policy_ref'::regclass
          AND contype = 'u'
          AND conkey = ARRAY[
              (SELECT attnum FROM pg_attribute WHERE attrelid = 'public.message_policy_ref'::regclass AND attname = 'message_id'),
              (SELECT attnum FROM pg_attribute WHERE attrelid = 'public.message_policy_ref'::regclass AND attname = 'display_order')
          ]::SMALLINT[]
          AND convalidated
          AND NOT condeferrable
          AND NOT condeferred
          AND pg_get_constraintdef(oid, TRUE)
              = 'UNIQUE (message_id, display_order)'
    ) THEN
        ALTER TABLE public.message_policy_ref
            ADD CONSTRAINT uq_message_policy_ref_order
            UNIQUE (message_id, display_order);
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.message_policy_ref'::regclass
          AND conname = 'chk_message_policy_ref_order'
          AND contype <> 'c'
    ) THEN
        RAISE EXCEPTION 'constraint chk_message_policy_ref_order exists with unexpected type';
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.message_policy_ref'::regclass
          AND conname = 'chk_message_policy_ref_order'
    ) THEN
        ALTER TABLE public.message_policy_ref
            ADD CONSTRAINT chk_message_policy_ref_order
            CHECK (display_order >= 1);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_current_policy'::regclass
          AND contype = 'p'
    ) THEN
        ALTER TABLE public.chat_current_policy
            ADD CONSTRAINT chat_current_policy_pkey
            PRIMARY KEY (room_id, policy_no);
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_current_policy'::regclass
          AND contype = 'p'
          AND conkey = ARRAY[
              (SELECT attnum FROM pg_attribute WHERE attrelid = 'public.chat_current_policy'::regclass AND attname = 'room_id'),
              (SELECT attnum FROM pg_attribute WHERE attrelid = 'public.chat_current_policy'::regclass AND attname = 'policy_no')
          ]::SMALLINT[]
          AND convalidated
          AND NOT condeferrable
          AND NOT condeferred
          AND pg_get_constraintdef(oid, TRUE)
              = 'PRIMARY KEY (room_id, policy_no)'
    ) THEN
        RAISE EXCEPTION 'chat_current_policy has an unexpected primary key';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_current_policy'::regclass
          AND conname = 'fk_chat_current_policy_room'
          AND contype <> 'f'
    ) THEN
        RAISE EXCEPTION 'constraint fk_chat_current_policy_room exists with unexpected type';
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_current_policy'::regclass
          AND contype = 'f'
          AND conkey = ARRAY[(
              SELECT attnum FROM pg_attribute
              WHERE attrelid = 'public.chat_current_policy'::regclass
                AND attname = 'room_id'
          )]::SMALLINT[]
          AND confrelid = 'public.chat_room'::regclass
          AND confkey = ARRAY[(
              SELECT attnum FROM pg_attribute
              WHERE attrelid = 'public.chat_room'::regclass
                AND attname = 'room_id'
          )]::SMALLINT[]
          AND confdeltype = 'c'
          AND convalidated
          AND NOT condeferrable
          AND NOT condeferred
          AND confupdtype = 'a'
          AND confmatchtype = 's'
          AND pg_get_constraintdef(oid, TRUE)
              = 'FOREIGN KEY (room_id) REFERENCES chat_room(room_id) ON DELETE CASCADE'
    ) THEN
        ALTER TABLE public.chat_current_policy
            ADD CONSTRAINT fk_chat_current_policy_room
            FOREIGN KEY (room_id)
            REFERENCES public.chat_room (room_id)
            ON DELETE CASCADE;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_current_policy'::regclass
          AND conname = 'fk_chat_current_policy_policy'
          AND contype <> 'f'
    ) THEN
        RAISE EXCEPTION 'constraint fk_chat_current_policy_policy exists with unexpected type';
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_current_policy'::regclass
          AND contype = 'f'
          AND conkey = ARRAY[(
              SELECT attnum FROM pg_attribute
              WHERE attrelid = 'public.chat_current_policy'::regclass
                AND attname = 'policy_no'
          )]::SMALLINT[]
          AND confrelid = 'public.policy'::regclass
          AND confkey = ARRAY[(
              SELECT attnum FROM pg_attribute
              WHERE attrelid = 'public.policy'::regclass
                AND attname = 'policy_no'
          )]::SMALLINT[]
          AND confdeltype = 'c'
          AND convalidated
          AND NOT condeferrable
          AND NOT condeferred
          AND confupdtype = 'a'
          AND confmatchtype = 's'
          AND pg_get_constraintdef(oid, TRUE)
              = 'FOREIGN KEY (policy_no) REFERENCES policy(policy_no) ON DELETE CASCADE'
    ) THEN
        ALTER TABLE public.chat_current_policy
            ADD CONSTRAINT fk_chat_current_policy_policy
            FOREIGN KEY (policy_no)
            REFERENCES public.policy (policy_no)
            ON DELETE CASCADE;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_current_policy'::regclass
          AND conname = 'uq_chat_current_policy_order'
          AND contype <> 'u'
    ) THEN
        RAISE EXCEPTION 'constraint uq_chat_current_policy_order exists with unexpected type';
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_current_policy'::regclass
          AND contype = 'u'
          AND conkey = ARRAY[
              (SELECT attnum FROM pg_attribute WHERE attrelid = 'public.chat_current_policy'::regclass AND attname = 'room_id'),
              (SELECT attnum FROM pg_attribute WHERE attrelid = 'public.chat_current_policy'::regclass AND attname = 'display_order')
          ]::SMALLINT[]
          AND convalidated
          AND NOT condeferrable
          AND NOT condeferred
          AND pg_get_constraintdef(oid, TRUE)
              = 'UNIQUE (room_id, display_order)'
    ) THEN
        ALTER TABLE public.chat_current_policy
            ADD CONSTRAINT uq_chat_current_policy_order
            UNIQUE (room_id, display_order);
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_current_policy'::regclass
          AND conname = 'chk_chat_current_policy_order'
          AND contype <> 'c'
    ) THEN
        RAISE EXCEPTION 'constraint chk_chat_current_policy_order exists with unexpected type';
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_current_policy'::regclass
          AND conname = 'chk_chat_current_policy_order'
    ) THEN
        ALTER TABLE public.chat_current_policy
            ADD CONSTRAINT chk_chat_current_policy_order
            CHECK (display_order >= 1);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.policy_scrap'::regclass
          AND contype = 'p'
    ) THEN
        ALTER TABLE public.policy_scrap
            ADD CONSTRAINT policy_scrap_pkey
            PRIMARY KEY (user_id, policy_no);
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.policy_scrap'::regclass
          AND contype = 'p'
          AND conkey = ARRAY[
              (SELECT attnum FROM pg_attribute WHERE attrelid = 'public.policy_scrap'::regclass AND attname = 'user_id'),
              (SELECT attnum FROM pg_attribute WHERE attrelid = 'public.policy_scrap'::regclass AND attname = 'policy_no')
          ]::SMALLINT[]
          AND convalidated
          AND NOT condeferrable
          AND NOT condeferred
          AND pg_get_constraintdef(oid, TRUE)
              = 'PRIMARY KEY (user_id, policy_no)'
    ) THEN
        RAISE EXCEPTION 'policy_scrap has an unexpected primary key';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.policy_scrap'::regclass
          AND conname = 'fk_policy_scrap_policy'
          AND contype <> 'f'
    ) THEN
        RAISE EXCEPTION 'constraint fk_policy_scrap_policy exists with unexpected type';
    ELSIF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.policy_scrap'::regclass
          AND contype = 'f'
          AND conkey = ARRAY[(
              SELECT attnum FROM pg_attribute
              WHERE attrelid = 'public.policy_scrap'::regclass
                AND attname = 'policy_no'
          )]::SMALLINT[]
          AND confrelid = 'public.policy'::regclass
          AND confkey = ARRAY[(
              SELECT attnum FROM pg_attribute
              WHERE attrelid = 'public.policy'::regclass
                AND attname = 'policy_no'
          )]::SMALLINT[]
          AND confdeltype = 'c'
          AND convalidated
          AND NOT condeferrable
          AND NOT condeferred
          AND confupdtype = 'a'
          AND confmatchtype = 's'
          AND pg_get_constraintdef(oid, TRUE)
              = 'FOREIGN KEY (policy_no) REFERENCES policy(policy_no) ON DELETE CASCADE'
    ) THEN
        ALTER TABLE public.policy_scrap
            ADD CONSTRAINT fk_policy_scrap_policy
            FOREIGN KEY (policy_no)
            REFERENCES public.policy (policy_no)
            ON DELETE CASCADE;
    END IF;
END;
$migration$;

-- CREATE INDEX IF NOT EXISTS는 동명 인덱스의 컬럼·정렬·predicate가 달라도
-- 건너뛴다. 잘못된 인덱스를 조용히 수용하지 않고 운영자가 확인할 수 있게 중단한다.
DO $migration$
BEGIN
    IF to_regclass('public.idx_chat_room_user_updated') IS NOT NULL
       AND NOT EXISTS (
            SELECT 1
            FROM pg_class AS index_relation
            JOIN pg_namespace AS index_namespace
              ON index_namespace.oid = index_relation.relnamespace
            JOIN pg_index AS index_catalog
              ON index_catalog.indexrelid = index_relation.oid
            JOIN pg_am AS access_method
              ON access_method.oid = index_relation.relam
            WHERE index_namespace.nspname = 'public'
              AND index_relation.relname = 'idx_chat_room_user_updated'
              AND index_catalog.indrelid = 'public.chat_room'::regclass
              AND access_method.amname = 'btree'
              AND index_catalog.indisvalid
              AND index_catalog.indisready
              AND NOT index_catalog.indisunique
              AND index_catalog.indnkeyatts = 2
              AND index_catalog.indnatts = 2
              AND index_catalog.indkey::TEXT = (
                  (SELECT attnum::TEXT FROM pg_attribute
                   WHERE attrelid = 'public.chat_room'::regclass
                     AND attname = 'user_id')
                  || ' ' ||
                  (SELECT attnum::TEXT FROM pg_attribute
                   WHERE attrelid = 'public.chat_room'::regclass
                     AND attname = 'updated_at')
              )
              AND index_catalog.indoption::TEXT = '0 3'
              AND index_catalog.indexprs IS NULL
              AND index_catalog.indpred IS NULL
              AND pg_get_indexdef(index_relation.oid)
                  = 'CREATE INDEX idx_chat_room_user_updated ON public.chat_room USING btree (user_id, updated_at DESC)'
       ) THEN
        RAISE EXCEPTION USING
            MESSAGE = 'index public.idx_chat_room_user_updated has an unexpected definition',
            HINT = 'Inspect and rename or rebuild the conflicting index before rerunning migration 015.';
    END IF;

    IF to_regclass('public.idx_chat_message_reply_to') IS NOT NULL
       AND NOT EXISTS (
            SELECT 1
            FROM pg_class AS index_relation
            JOIN pg_namespace AS index_namespace
              ON index_namespace.oid = index_relation.relnamespace
            JOIN pg_index AS index_catalog
              ON index_catalog.indexrelid = index_relation.oid
            JOIN pg_am AS access_method
              ON access_method.oid = index_relation.relam
            WHERE index_namespace.nspname = 'public'
              AND index_relation.relname = 'idx_chat_message_reply_to'
              AND index_catalog.indrelid = 'public.chat_message'::regclass
              AND access_method.amname = 'btree'
              AND index_catalog.indisvalid
              AND index_catalog.indisready
              AND NOT index_catalog.indisunique
              AND index_catalog.indnkeyatts = 1
              AND index_catalog.indnatts = 1
              AND index_catalog.indkey::TEXT = (
                  SELECT attnum::TEXT
                  FROM pg_attribute
                  WHERE attrelid = 'public.chat_message'::regclass
                    AND attname = 'reply_to_message_id'
              )
              AND index_catalog.indoption::TEXT = '0'
              AND index_catalog.indexprs IS NULL
              AND pg_get_expr(index_catalog.indpred, index_catalog.indrelid)
                  = '(reply_to_message_id IS NOT NULL)'
              AND pg_get_indexdef(index_relation.oid)
                  = 'CREATE INDEX idx_chat_message_reply_to ON public.chat_message USING btree (reply_to_message_id) WHERE (reply_to_message_id IS NOT NULL)'
       ) THEN
        RAISE EXCEPTION USING
            MESSAGE = 'index public.idx_chat_message_reply_to has an unexpected definition',
            HINT = 'Inspect and rename or rebuild the conflicting index before rerunning migration 015.';
    END IF;
END;
$migration$;

CREATE INDEX IF NOT EXISTS idx_chat_room_user_updated
    ON public.chat_room (user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_chat_message_reply_to
    ON public.chat_message (reply_to_message_id)
    WHERE reply_to_message_id IS NOT NULL;

COMMIT;
