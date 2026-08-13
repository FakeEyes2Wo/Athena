import { z } from "zod"
import { ArtifactRef, NonBlankText } from "./contracts.js"

/** 数据集卡片——数据集指纹、schema 与划分。 */
export const DataCardSchema = z.object({
  dataset_ref: ArtifactRef,
  fingerprint: z.string(),
  schema_ref: ArtifactRef,
  split_manifest_ref: ArtifactRef.nullable().default(null),
})
export type DataCard = z.infer<typeof DataCardSchema>

/** 单列统计摘要。 */
export const ColumnSummarySchema = z.object({
  name: z.string(),
  dtype: z.string(),
  missing_rate: z.number().default(0.0),
  n_unique: z.number().nullable().default(null),
  sample_values: z.array(z.string()).default([]),
  processing: z.string().default(""),
})
export type ColumnSummary = z.infer<typeof ColumnSummarySchema>

/** 数据集画像。 */
export const DataProfileSchema = z.object({
  row_count: z.number(),
  col_count: z.number(),
  columns: z.array(ColumnSummarySchema).default([]),
  missing_rate: z.number().default(0.0),
  task_type_hint: z.string().default(""),
  target_col: z.string().nullable().default(null),
  issue_summary: z.string().default(""),
})
export type DataProfile = z.infer<typeof DataProfileSchema>

/** 评估指标规格——名称与优化方向。 */
export const MetricSpecSchema = z.object({
  name: NonBlankText,
  direction: z.enum(["maximize", "minimize"]),
})
export type MetricSpec = z.infer<typeof MetricSpecSchema>

/** 任务元数据。 */
export const TaskMetaDataSchema = z.object({
  task_type: z.string(),
  data_type: z.string(),
  target_vars: z.array(z.string()).default([]),
  primary_metric: MetricSpecSchema,
  constraints: z.array(z.string()).default([]),
})
export type TaskMetaData = z.infer<typeof TaskMetaDataSchema>
