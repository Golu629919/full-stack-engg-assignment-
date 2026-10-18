run:
	python3 -m newtonite
test:
	python3 -m unittest discover -s tests -t . -v
reset:
	rm -f newtonite.db newtonite.db-wal newtonite.db-shm
