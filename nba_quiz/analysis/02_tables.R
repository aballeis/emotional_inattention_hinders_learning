library(tidyverse)
library(sandwich)

out_dir <- "tables_and_charts"
paper_dir <- "../../paper/Tables"
dir.create(out_dir, showWarnings = FALSE)

write_table <- function(lines, name) {
  file <- file.path(out_dir, name)
  writeLines(lines, file)
  if (dir.exists(paper_dir)) invisible(file.copy(file, paper_dir, overwrite = TRUE))
}

participants <- read_csv("data/built/participant_level_data.csv", show_col_types = FALSE)
d <- participants %>%
  filter(in_sample) %>%
  mutate(pref_win_c = pref_win - mean(pref_win), pref_loss_c = pref_loss - mean(pref_loss))

# ---- Effect of the assigned review ----

prereg <- exp_score_dif ~ assigned_loss + pref_win_c + pref_loss_c + assigned_loss:pref_win_c + assigned_loss:pref_loss_c
fit <- function(f, x) {
  m <- lm(f, x)
  V <- vcovHC(m, type = "HC1")
  b <- coef(m)
  lost <- c(1, 1, rep(0, length(b) - 2))
  p <- 2 * pt(-abs(b[2] / sqrt(V[2, 2])), df.residual(m))
  list(won = b[1], won_se = sqrt(V[1, 1]), lost = sum(lost * b), lost_se = sqrt(drop(t(lost) %*% V %*% lost)),
       dif = b[2], dif_se = sqrt(V[2, 2]), p = p, n = nobs(m))
}
models <- list(fit(prereg, d),
               fit(exp_score_dif ~ assigned_loss, filter(d, review_pref == "win")),
               fit(exp_score_dif ~ assigned_loss, filter(d, review_pref == "loss")),
               fit(exp_score_dif ~ assigned_loss, filter(d, review_pref == "indifferent")))

stars <- \(p) case_when(p < .01 ~ "$^{***}$", p < .05 ~ "$^{**}$", p < .1 ~ "$^{*}$", TRUE ~ "")
row <- \(label, f) paste0("      ", label, " & ", paste(map_chr(models, f), collapse = " & "), "\\\\")

write_table(c(
  "\\begin{table}[tbp]\\centering",
  "  \\begin{threeparttable}\\small",
  "    \\caption{Effect of the assigned review on the change in the expected number of questions won}",
  "    \\label{tab:nba_experiment}",
  "    \\begin{tabular}{lcccc}",
  "      \\toprule",
  "       & \\multicolumn{4}{c}{Change in expected questions won, Round 2 minus Round 1}\\\\",
  "      Sample & All & Prefer win & Prefer loss & No preference\\\\",
  "       & (1) & (2) & (3) & (4)\\\\",
  "      \\midrule",
  row("Won question reviewed", \(m) sprintf("%.3f", m$won)),
  row("", \(m) sprintf("(%.3f)", m$won_se)),
  row("Lost question reviewed", \(m) sprintf("%.3f", m$lost)),
  row("", \(m) sprintf("(%.3f)", m$lost_se)),
  row("Difference", \(m) sprintf("%.3f%s", m$dif, stars(m$p))),
  row("", \(m) sprintf("(%.3f)", m$dif_se)),
  "       \\\\",
  row("Observations", \(m) as.character(m$n)),
  "      \\bottomrule",
  "    \\end{tabular}",
  "    \\begin{tablenotes}",
  "      \\footnotesize",
  paste("      \\item \\textit{Note:} Dependent variable: change in the participant's expected number of questions won out of five",
        "(computed against the pretest opponent pool) from Round 1 to Round 2. Rows: the mean change for participants assigned",
        "to review the won question and for those assigned the lost question, and the difference between them, from a regression",
        "of the change on an indicator for being assigned the lost question. Column (1) controls for the stated review preference",
        "(centered) and its interactions with the assignment; columns (2)--(4) split the sample by stated review preference.",
        "HC1 standard errors in parentheses. $^{*}$ $p<0.10$, $^{**}$ $p<0.05$, $^{***}$ $p<0.01$."),
  "    \\end{tablenotes}",
  "  \\end{threeparttable}",
  "\\end{table}"), "nba_review_effect.tex")

# ---- Sample flow ----

steps <- c(passed_attention = "Passed the attention check",
           entered_app = "Entered the main study",
           passed_comprehension = "Passed the comprehension check (at most three attempts)",
           completed_r1 = "Completed Round 1",
           win_and_loss_r1 = "At least one won and one lost question in Round 1",
           completed_r2 = "Completed Round 2",
           closing_questions = "Answered the closing questions",
           no_lookup = "Did not report looking up NBA games (analysis sample)")
remaining <- accumulate(names(steps), \(s, v) s & participants[[v]], .init = rep(TRUE, nrow(participants))) %>% map_int(sum)
flow <- tibble(step = c("Started the study", steps), n = remaining)

write_table(c(
  "\\begin{table}[htbp]\\centering",
  "  \\begin{threeparttable}",
  "    \\caption{Sample flow of the NBA prediction study}",
  "    \\label{tab:nba_sample_flow}",
  "    \\begin{tabular}{lr}",
  "      \\toprule",
  "      Step & Participants remaining\\\\",
  "      \\midrule",
  sprintf("      %s & %s\\\\", flow$step, format(flow$n, big.mark = ",", trim = TRUE)),
  "      \\bottomrule",
  "    \\end{tabular}",
  "    \\begin{tablenotes}",
  "      \\footnotesize",
  "      \\item \\textit{Note:} Steps in the order in which they occurred in the study; each row applies the stated criterion to the participants remaining from the row above.",
  "    \\end{tablenotes}",
  "  \\end{threeparttable}",
  "\\end{table}"), "nba_sample_flow.tex")
