library(tidyverse)

out_dir <- "tables_and_charts"
paper_dir <- "../../paper/Tables"
dir.create(out_dir, showWarnings = FALSE)

d <- read_csv("data/built/survey_sample.csv", show_col_types = FALSE)
has <- !is.na(d$rating)
groups <- list("Below median" = has & !d$high_rating, "Median or above" = has & d$high_rating, "All" = rep(TRUE, nrow(d)))
row <- \(label, answer) sprintf("      \\quad %s & %s\\\\", label,
  paste(map_chr(groups, \(g) sprintf("%d (%.0f)", sum(answer & g), 100 * sum(answer & g) / sum(g))), collapse = " & "))
fisher <- fisher.test(table(d$high_rating[has], d$helps_more[has] == "lost"))$p.value
f_p <- \(p) if (p < .001) "$p<0.001$" else sprintf("$p=%.2f$", p)

file <- file.path(out_dir, "chess_survey_by_rating.tex")
writeLines(c(
  "\\begin{table}[tbp]\\centering",
  "  \\begin{threeparttable}",
  "    \\caption{Survey answers by the respondent's Chess.com rating}",
  "    \\label{tab:survey_by_rating}",
  "    \\begin{tabular}{lccc}",
  "      \\toprule",
  paste0("       & ", paste(sprintf("\\multicolumn{1}{c}{%s}", names(groups)), collapse = " & "), "\\\\"),
  "      \\midrule",
  "      \\textit{More helpful to review} & & & \\\\",
  row("A loss", d$helps_more == "lost"), row("A win", d$helps_more == "won"),
  row("Both equally", d$helps_more == "both"), row("Neither", d$helps_more == "neither"),
  "      \\textit{More pleasant to review} & & & \\\\",
  row("A win", d$feels_better == "won"), row("A loss", d$feels_better == "lost"), row("No difference", d$feels_better == "nodiff"),
  "      \\textit{Wish to shift review time} & & & \\\\",
  row("Toward losses", d$shift > 0), row("Toward wins", d$shift < 0), row("Same change for both", d$shift == 0),
  "       \\\\",
  paste0("      Respondents & ", paste(map_int(groups, sum), collapse = " & "), "\\\\"),
  "      \\bottomrule",
  "    \\end{tabular}",
  "    \\begin{tablenotes}",
  "      \\footnotesize",
  paste0("      \\item \\textit{Note:} Counts, with percentages of the column in parentheses, by the respondent's rating relative to the median. ",
         sprintf("The rating is the respondent's self-reported Chess.com rating; %d of the %d respondents reported one (median %s), ",
                 sum(has), nrow(d), format(median(d$rating, na.rm = TRUE), big.mark = ",")),
         "the others entered 0, 1 or 3, which we treat as missing. Fisher's exact test of ``a loss'' against the other answers ",
         "to the first question, by rating group: ", f_p(fisher), "."),
  "    \\end{tablenotes}",
  "  \\end{threeparttable}",
  "\\end{table}"), file)
if (dir.exists(paper_dir)) invisible(file.copy(file, paper_dir, overwrite = TRUE))
