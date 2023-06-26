echo
echo "### setup/set env started"
echo

if command -v module &> /dev/null
then
    module purge
    module load unstable git python gcc hpe-mpi petsc-complex py-petsc4py-complex
else
    if command -v conda &> /dev/null
    then
        if conda env list | grep bfs_env >/dev/null 2>/dev/null;
        then
            echo "Conda setup: Done"
            conda activate bfs_env

            echo
            echo "If you want to purge the current env, follow the steps below: "
            echo "1. conda deactivate"
            echo "2. conda remove -y --name bfs_env --all"
            echo
        else
            # environment with python 3.10.8
            # (the latest one tested in BBP's supercomputer at the time of AstroVascPy's release)
            conda create -y --name bfs_env python=3.10.8
            conda activate bfs_env
            conda install -y pip

            if [[ $OSTYPE == 'darwin'* ]];
            then
                sed -i '' 's/"mpi4py"/#"mpi4py"/g' setup.py
            else
                sed -i 's/"mpi4py"/#"mpi4py"/g' setup.py
            fi

            conda install -y -c conda-forge mpi mpi4py "petsc=*=*complex*" "petsc4py=*=*complex*"
        fi
    else
        echo
        echo "Please install Conda, and then proceed to the installation of AstroVascPy."
        echo "!! EXITING !!"
        echo
        exit
    fi
fi

if command -v module &> /dev/null
then
    echo
    echo "### python-venv [Python Virtual Environment]"
    echo
    # Export proxy configuration before trying to 'pip install' from a compute node
    export HTTP_PROXY="http://bbpproxy.epfl.ch:80/"
    export HTTPS_PROXY="http://bbpproxy.epfl.ch:80/"
    export http_proxy="http://bbpproxy.epfl.ch:80/"
    export https_proxy="http://bbpproxy.epfl.ch:80/"

    if [ -d "python-venv" ]
    then
        echo "python-venv already set"
        source python-venv/bin/activate
    else
        python3 -m venv --prompt astrovascpy python-venv
        source python-venv/bin/activate
        python3 -m pip install --upgrade pip
    fi
    pip3 install -e .
else
    conda_bin=`conda info | grep "active env location" | grep -o "/.*"`/bin
    $conda_bin/pip install -e .
fi

# Backend solver/library for the linear systems
# petsc or scipy
export BACKEND_SOLVER_BFS='scipy'

# Run the SciPy solver and compare the result with the PETSc one [which is the default]!
# 0 : False / 1 : True
export DEBUG_BFS=0

# Show PETSc progress or not
# 0 : False / 1 : True
export VERBOSE_BFS=0

echo
echo "### setup finished"
echo

echo
echo "--> Now you could go to the examples folder and run your first example as: "
echo "--> conda_bin/mpirun (or srun) -n number_of_mpi_tasks python compute_static_flow_pressure.py"
echo