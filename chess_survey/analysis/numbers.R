library(tidyverse)

paper_dir <- "../../paper"

all <- read_csv("data/survey_data.csv", show_col_types = FALSE, col_types = cols(.default = "c"))
d <- read_csv("data/built/survey_sample.csv", show_col_types = FALSE)
labels <- read_csv("data/llm_labels_fable-5-1.csv", show_col_types = FALSE)
labels_robust <- read_csv("data/llm_labels_haiku-4-5.csv", show_col_types = FALSE)

n <- nrow(d)
pc <- \(k, m = n) formatC(100 * k / m, format = "f", digits = 0)
f_p <- \(p) if (p < .001) "$p<0.001$" else sprintf("$p=%.3f$", p)
none_or <- \(k, m = n) if (k == 0) "none" else paste(pc(k, m), "percent")

tex <- if (dir.exists(paper_dir)) {
  list.files(paper_dir, "\\.tex$", full.names = TRUE) %>%
    set_names(basename) %>%
    map(\(f) str_squish(paste(readLines(f, warn = FALSE), collapse = " ")))
}
rows <- list()
add <- function(section, file, statistic, value, raw = "", snippet = value) {
  found <- if (is.null(tex)) "paper not found" else if (str_detect(tex[[file]], fixed(snippet))) "yes" else paste0("NO: expected `", snippet, "`")
  rows[[length(rows) + 1]] <<- tibble(section, location = str_remove(file, "\\.tex$"), statistic, value, raw, found)
}
intro <- "Sec 1 Intro.tex"
chess <- "Sec 3 Chess.tex"
llm <- "Sec A3 Classifier Prompt.tex"

k <- \(x) sum(x, na.rm = TRUE)
helps <- map_int(set_names(c("lost", "won", "both", "neither")), \(a) k(d$helps_more == a))
pleasant <- map_int(set_names(c("won", "lost", "nodiff")), \(a) k(d$feels_better == a))
tests <- c(helpful = binom.test(helps[["lost"]], helps[["lost"]] + helps[["won"]])$p.value,
           pleasant = binom.test(pleasant[["won"]], pleasant[["won"]] + pleasant[["lost"]])$p.value,
           shift = binom.test(k(d$shift > 0), k(d$shift != 0))$p.value)

# ---- Sample ----

add("Sample", intro, "Analysis sample", sprintf("$N=%d$", n))
add("Sample", chess, "Respondents / analysis sample (footnote)",
    sprintf("Of %d respondents, %d completed the survey and play on Chess.com at least once a month", nrow(all), n))
add("Sample", chess, "Analysis sample (text)", sprintf("The %d participants answered three preregistered questions", n))

# ---- Preregistered questions ----

add("Answers", intro, "Loss more helpful; win more helpful",
    sprintf("A majority (%s percent) reported that reviewing a loss is strictly more beneficial for skill development than reviewing a win, and %s reported the reverse",
            pc(helps[["lost"]]), if (helps[["won"]] == 0) "none" else pc(helps[["won"]])))
add("Answers", intro, "Loss more pleasant; win more pleasant",
    sprintf("only %s percent reported that reviewing a loss is strictly more pleasurable than reviewing a win, while a majority (%s percent) reported the reverse",
            pc(pleasant[["lost"]]), pc(pleasant[["won"]])))
add("Answers", chess, "More helpful: loss / win / both / neither",
    sprintf("%s percent said a loss, %s said a win, and the rest said both are equally helpful (or, in one case, neither)", pc(helps[["lost"]]), none_or(helps[["won"]])),
    paste(helps, collapse = " / "))
add("Answers", chess, "More pleasant: win / loss / no difference",
    sprintf("%s percent said a win, %s percent a loss, and %s percent saw no difference", pc(pleasant[["won"]]), pc(pleasant[["lost"]]), pc(pleasant[["nodiff"]])),
    paste(pleasant, collapse = " / "))
add("Answers", chess, "Wish to spend more / less time on losses and wins; shift",
    sprintf("%s percent wish they spent more time on losses and %s less, whereas %s percent wish they spent more time on wins and %s percent less; %s percent wish for a shift toward losses relative to wins, %s percent for the reverse, and the remaining %s percent for the same change for both",
            pc(k(d$time_losses == 1)), if (k(d$time_losses == -1) == 0) "none" else paste(pc(k(d$time_losses == -1)), "percent"),
            pc(k(d$time_wins == 1)), pc(k(d$time_wins == -1)), pc(k(d$shift > 0)), pc(k(d$shift < 0)), pc(k(d$shift == 0))),
    sprintf("losses more/less %d/%d; wins more/less %d/%d; shift +/-/0 %d/%d/%d", k(d$time_losses == 1), k(d$time_losses == -1),
            k(d$time_wins == 1), k(d$time_wins == -1), k(d$shift > 0), k(d$shift < 0), k(d$shift == 0)))
add("Answers", chess, "Preregistered binomial tests: helpful / pleasant / shift",
    sprintf("(%s, %s and %s)", f_p(tests[["helpful"]]), f_p(tests[["pleasant"]]), f_p(tests[["shift"]])),
    paste(sprintf("%.2g", tests), collapse = " / "))

# ---- Rating ----

has <- !is.na(d$rating)
fisher <- fisher.test(table(d$high_rating[has], d$helps_more[has] == "lost"))$p.value
add("Rating", "Sec A2 Figures Tables.tex", "Loss more helpful, below / at or above median rating (Table; Fisher test)",
    sprintf("%s / %s percent; Fisher p = %.3f", pc(k(d$helps_more[has & !d$high_rating] == "lost"), k(has & !d$high_rating)),
            pc(k(d$helps_more[has & d$high_rating] == "lost"), k(has & d$high_rating)), fisher),
    sprintf("%d rated, median %s", sum(has), median(d$rating, na.rm = TRUE)), snippet = "survey_by_rating")

# ---- LLM labels ----

cats <- c("learn", "unsure", "curious", "time", "avoid", "savor", "gametype")
agree <- inner_join(labels, labels_robust, by = "response_id", suffix = c("", "_r"))
rates <- map_dbl(cats, \(v) mean(agree[[v]] == agree[[paste0(v, "_r")]]))
add("LLM labels", llm, "Labeled survey answers", sprintf("(Section~\\ref{sec:survey}, $N=%d$)", nrow(labels)))
add("LLM labels", llm, "Agreement of Claude Haiku 4.5 with Claude Fable 5.1 labels, by category (min to max)",
    sprintf("%s to %s percent of the survey answers", pc(min(rates), 1), pc(max(rates), 1)), paste(formatC(100 * rates, format = "f", digits = 1), collapse = ", "))

# ---- Write ----

out <- bind_rows(rows)
md <- c("# Numbers check: survey of chess players", "",
        "Generated by `analysis/numbers.R`. Every statistic about the survey quoted in the paper text, with where it appears.",
        "Tests are the preregistered two-sided exact binomial tests, excluding those without a preference. \"In paper\" checks that",
        "the text in the Value column appears verbatim in that section of the paper.", "",
        sprintf("**%d of %d statistics found in the paper text.**", sum(out$found == "yes"), nrow(out)), "")
for (sec in unique(out$section)) {
  s <- filter(out, section == sec)
  md <- c(md, paste("##", sec), "", "| Location | Statistic | Value | Unrounded | In paper |", "|---|---|---|---|---|",
          sprintf("| %s | %s | %s | %s | %s |", s$location, s$statistic, s$value, s$raw, s$found), "")
}
dir.create("tables_and_charts", showWarnings = FALSE)
writeLines(md, "tables_and_charts/numbers_check.md")
cat(sum(out$found == "yes"), "of", nrow(out), "found in paper\n")
