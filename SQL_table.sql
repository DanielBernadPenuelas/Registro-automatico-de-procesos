CREATE TABLE ops.TASK_LOG
    (
        ID              INT             IDENTITY(1,1)   NOT NULL,
        TASK_NAME       NVARCHAR(50)                    NOT NULL,
        START_DATE      DATETIME                        NOT NULL
            CONSTRAINT DF_TASK_LOG_START_DATE DEFAULT (SYSUTCDATETIME()),
        END_DATE        DATETIME                            NULL,
        PROGRAM_STATE   NVARCHAR(2)                     NOT NULL
            CONSTRAINT DF_TASK_LOG_PROGRAM_STATE DEFAULT (N'RN'),
        MENSAGE_ERROR   NVARCHAR(3000)                      NULL,
 
        CONSTRAINT PK_TASK_LOG PRIMARY KEY CLUSTERED (ID),
 
        CONSTRAINT CK_TASK_LOG_PROGRAM_STATE
            CHECK (PROGRAM_STATE IN (N'RN', N'OK', N'KO', N'WN')),
 
        CONSTRAINT CK_TASK_LOG_DATES
            CHECK (END_DATE IS NULL OR END_DATE >= START_DATE)
    );