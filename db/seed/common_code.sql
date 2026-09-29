-- YouthLink common-code seed data
-- Source: API코드정보.xlsx (코드정보 sheet)
--
-- Note:
-- The spreadsheet uses `bizPrdSecd` for the business-period code group,
-- while the supplied API response uses `bizPrdSeCd`.
-- This seed normalizes that one group name to `bizPrdSeCd` so it matches
-- the actual API field used by the ETL pipeline.

BEGIN;

INSERT INTO common_code (code, code_group, code_name)
VALUES
    ('0054001', 'pvsnInstGroupCd', '중앙부처'),
    ('0054002', 'pvsnInstGroupCd', '지자체'),
    ('0042001', 'plcyPvsnMthdCd', '인프라 구축'),
    ('0042002', 'plcyPvsnMthdCd', '프로그램'),
    ('0042003', 'plcyPvsnMthdCd', '직접대출'),
    ('0042004', 'plcyPvsnMthdCd', '공공기관'),
    ('0042005', 'plcyPvsnMthdCd', '계약(위탁운영)'),
    ('0042006', 'plcyPvsnMthdCd', '보조금'),
    ('0042007', 'plcyPvsnMthdCd', '대출보증'),
    ('0042008', 'plcyPvsnMthdCd', '공적보험'),
    ('0042009', 'plcyPvsnMthdCd', '조세지출'),
    ('0042010', 'plcyPvsnMthdCd', '바우처'),
    ('0042011', 'plcyPvsnMthdCd', '정보제공'),
    ('0042012', 'plcyPvsnMthdCd', '경제적 규제'),
    ('0042013', 'plcyPvsnMthdCd', '기타'),
    ('0044001', 'plcyAprvSttsCd', '신청'),
    ('0044002', 'plcyAprvSttsCd', '승인'),
    ('0044003', 'plcyAprvSttsCd', '반려'),
    ('0044004', 'plcyAprvSttsCd', '임시저장'),
    ('0057001', 'aplyPrdSeCd', '특정기간'),
    ('0057002', 'aplyPrdSeCd', '상시'),
    ('0057003', 'aplyPrdSeCd', '마감'),
    ('0056001', 'bizPrdSeCd', '특정기간'),
    ('0056002', 'bizPrdSeCd', '기타'),
    ('0055001', 'mrgSttsCd', '기혼'),
    ('0055002', 'mrgSttsCd', '미혼'),
    ('0055003', 'mrgSttsCd', '제한없음'),
    ('0043001', 'earnCndSeCd', '무관'),
    ('0043002', 'earnCndSeCd', '연소득'),
    ('0043003', 'earnCndSeCd', '기타'),
    ('0011001', 'plcyMajorCd', '인문계열'),
    ('0011002', 'plcyMajorCd', '사회계열'),
    ('0011003', 'plcyMajorCd', '상경계열'),
    ('0011004', 'plcyMajorCd', '이학계열'),
    ('0011005', 'plcyMajorCd', '공학계열'),
    ('0011006', 'plcyMajorCd', '예체능계열'),
    ('0011007', 'plcyMajorCd', '농산업계열'),
    ('0011008', 'plcyMajorCd', '기타'),
    ('0011009', 'plcyMajorCd', '제한없음'),
    ('0013001', 'jobCd', '재직자'),
    ('0013002', 'jobCd', '자영업자'),
    ('0013003', 'jobCd', '미취업자'),
    ('0013004', 'jobCd', '프리랜서'),
    ('0013005', 'jobCd', '일용근로자'),
    ('0013006', 'jobCd', '(예비)창업자'),
    ('0013007', 'jobCd', '단기근로자'),
    ('0013008', 'jobCd', '영농종사자'),
    ('0013009', 'jobCd', '기타'),
    ('0013010', 'jobCd', '제한없음'),
    ('0049001', 'schoolCd', '고졸 미만'),
    ('0049002', 'schoolCd', '고교 재학'),
    ('0049003', 'schoolCd', '고졸 예정'),
    ('0049004', 'schoolCd', '고교 졸업'),
    ('0049005', 'schoolCd', '대학 재학'),
    ('0049006', 'schoolCd', '대졸 예정'),
    ('0049007', 'schoolCd', '대학 졸업'),
    ('0049008', 'schoolCd', '석·박사'),
    ('0049009', 'schoolCd', '기타'),
    ('0049010', 'schoolCd', '제한없음'),
    ('0014001', 'sbizCd', '중소기업'),
    ('0014002', 'sbizCd', '여성'),
    ('0014003', 'sbizCd', '기초생활수급자'),
    ('0014004', 'sbizCd', '한부모가정'),
    ('0014005', 'sbizCd', '장애인'),
    ('0014006', 'sbizCd', '농업인'),
    ('0014007', 'sbizCd', '군인'),
    ('0014008', 'sbizCd', '지역인재'),
    ('0014009', 'sbizCd', '기타'),
    ('0014010', 'sbizCd', '제한없음')
ON CONFLICT (code) DO UPDATE
SET
    code_group = EXCLUDED.code_group,
    code_name  = EXCLUDED.code_name;

COMMIT;
