# Template: Makefile for any uv repo. Render with templates/render.py.
# placeholders: entry, default_job
#   entry        command run by `make run`, e.g. hk templates or python -m mypkg
#   default_job  sbatch stem under slurm/ used when JOB is not given
.RECIPEPREFIX = >
JOB ?= {{default_job}}
.PHONY: run test slurm
run:
> uv run {{entry}} $(ARGS)
test:
> uv run pytest -q
slurm:
> mkdir -p logs
> sbatch slurm/$(JOB).sbatch
